"""PostgreSQL immutable Message history and branch-path reads."""

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import exists, select, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from packages.contracts.public import TenantContext, validation_error
from packages.domain.public import (
    MessageRecord,
    MessageRole,
    parse_message_content_parts,
)
from packages.infrastructure.database.models import (
    ChatMessageModel,
    ChatSessionModel,
)
from packages.infrastructure.database.uow import TenantUnitOfWork


@dataclass(frozen=True, slots=True)
class _MessageCursor:
    session_id: UUID
    tip_id: UUID
    after_id: UUID
    after_depth: int
    requested_branch_id: UUID | None


class SqlAlchemyMessageHistoryStore:
    """Read one immutable parent chain with a Tip-frozen pagination cursor."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_session_messages(
        self,
        context: TenantContext,
        *,
        user_id: UUID,
        session_id: UUID,
        limit: int,
        cursor: str | None,
        branch_id: UUID | None,
    ) -> tuple[list[MessageRecord], str | None] | None:
        if not 1 <= limit <= 200:
            raise validation_error("limit must be between 1 and 200")
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            chat_session = await session.scalar(
                select(ChatSessionModel).where(
                    ChatSessionModel.tenant_id == tenant_id,
                    ChatSessionModel.user_id == user_id,
                    ChatSessionModel.id == session_id,
                    ChatSessionModel.status != "DELETED",
                )
            )
            if chat_session is None:
                return None

            decoded = _decode_cursor(cursor) if cursor is not None else None
            if decoded is not None:
                if (
                    decoded.session_id != session_id
                    or decoded.requested_branch_id != branch_id
                ):
                    raise validation_error("Pagination cursor is invalid.")
                tip_id = decoded.tip_id
                after_depth = await _message_depth(
                    session,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    tip_id=tip_id,
                    message_id=decoded.after_id,
                )
                if after_depth is None or after_depth != decoded.after_depth:
                    raise validation_error("Pagination cursor is invalid.")
            else:
                tip_id = (
                    await _branch_tip(
                        session,
                        tenant_id=tenant_id,
                        session_id=session_id,
                        branch_id=branch_id,
                    )
                    if branch_id is not None
                    else chat_session.cursor_message_id
                )
                after_depth = None
            if tip_id is None:
                return [], None

            rows = (
                (
                    await session.execute(
                        _MESSAGE_PATH_QUERY,
                        {
                            "tenant_id": str(tenant_id),
                            "session_id": str(session_id),
                            "tip_id": str(tip_id),
                            "after_depth": after_depth,
                            "limit_plus_one": limit + 1,
                        },
                    )
                )
                .mappings()
                .all()
            )
            page = rows[:limit]
            records = [_message_record(row) for row in page]
            next_cursor = None
            if len(rows) > limit and page:
                last = page[-1]
                next_cursor = _encode_cursor(
                    _MessageCursor(
                        session_id=session_id,
                        tip_id=tip_id,
                        after_id=cast(UUID, last["id"]),
                        after_depth=int(cast(int, last["depth"])),
                        requested_branch_id=branch_id,
                    )
                )
            return records, next_cursor


async def _branch_tip(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    session_id: UUID,
    branch_id: UUID,
) -> UUID | None:
    message = aliased(ChatMessageModel)
    child = aliased(ChatMessageModel)
    statement = (
        select(message.id)
        .where(
            message.tenant_id == tenant_id,
            message.session_id == session_id,
            message.branch_id == branch_id,
            ~exists(
                select(1).where(
                    child.tenant_id == tenant_id,
                    child.session_id == session_id,
                    child.branch_id == branch_id,
                    child.parent_message_id == message.id,
                )
            ),
        )
        .order_by(message.created_at.desc(), message.id.desc())
        .limit(2)
    )
    tips = list((await session.scalars(statement)).all())
    if len(tips) > 1:
        raise RuntimeError("Message branch contains multiple immutable tips")
    return tips[0] if tips else None


async def _message_depth(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    session_id: UUID,
    tip_id: UUID,
    message_id: UUID,
) -> int | None:
    depth = await session.scalar(
        _MESSAGE_DEPTH_QUERY,
        {
            "tenant_id": str(tenant_id),
            "session_id": str(session_id),
            "tip_id": str(tip_id),
            "message_id": str(message_id),
        },
    )
    return int(depth) if depth is not None else None


def _message_record(row: RowMapping) -> MessageRecord:
    try:
        content_parts = parse_message_content_parts(row["content_parts_json"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Stored Message content is invalid") from exc
    return MessageRecord(
        id=cast(UUID, row["id"]),
        tenant_id=cast(UUID, row["tenant_id"]),
        session_id=cast(UUID, row["session_id"]),
        branch_id=cast(UUID | None, row["branch_id"]),
        parent_message_id=cast(UUID | None, row["parent_message_id"]),
        role=cast(MessageRole, row["role"]),
        content_parts=content_parts,
        content_schema_version=cast(str, row["content_schema_version"]),
        source_run_id=cast(UUID | None, row["source_run_id"]),
        created_at=cast(datetime, row["created_at"]),
        created_by=cast(UUID, row["created_by"]),
    )


def _encode_cursor(cursor: _MessageCursor) -> str:
    payload = json.dumps(
        {
            "session_id": str(cursor.session_id),
            "tip_id": str(cursor.tip_id),
            "after_id": str(cursor.after_id),
            "after_depth": cursor.after_depth,
            "requested_branch_id": (
                str(cursor.requested_branch_id)
                if cursor.requested_branch_id is not None
                else None
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(value: str) -> _MessageCursor:
    try:
        padding = "=" * (-len(value) % 4)
        decoded: object = json.loads(base64.urlsafe_b64decode(value + padding))
        if not isinstance(decoded, dict):
            raise TypeError
        payload = cast(dict[str, object], decoded)
        branch_value = payload["requested_branch_id"]
        after_depth = payload["after_depth"]
        if not isinstance(after_depth, int) or after_depth < 0:
            raise TypeError
        return _MessageCursor(
            session_id=UUID(cast(str, payload["session_id"])),
            tip_id=UUID(cast(str, payload["tip_id"])),
            after_id=UUID(cast(str, payload["after_id"])),
            after_depth=after_depth,
            requested_branch_id=(
                UUID(branch_value) if isinstance(branch_value, str) else None
            ),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise validation_error("Pagination cursor is invalid.") from exc


_MESSAGE_PATH_QUERY = text("""
    WITH RECURSIVE message_path AS (
        SELECT m.id, m.tenant_id, m.session_id, m.branch_id,
               m.parent_message_id, m.role, m.content_parts_json,
               m.content_schema_version, m.source_run_id, m.created_at,
               m.created_by, 0::bigint AS depth
        FROM chat_message AS m
        WHERE m.tenant_id = CAST(:tenant_id AS uuid)
          AND m.session_id = CAST(:session_id AS uuid)
          AND m.id = CAST(:tip_id AS uuid)
        UNION ALL
        SELECT parent.id, parent.tenant_id, parent.session_id, parent.branch_id,
               parent.parent_message_id, parent.role, parent.content_parts_json,
               parent.content_schema_version, parent.source_run_id,
               parent.created_at, parent.created_by, child.depth + 1
        FROM chat_message AS parent
        JOIN message_path AS child
          ON parent.tenant_id = child.tenant_id
         AND parent.session_id = child.session_id
         AND parent.id = child.parent_message_id
    )
    SELECT id, tenant_id, session_id, branch_id, parent_message_id, role,
           content_parts_json, content_schema_version, source_run_id,
           created_at, created_by, depth
    FROM message_path
    WHERE CAST(:after_depth AS bigint) IS NULL
       OR depth < CAST(:after_depth AS bigint)
    ORDER BY depth DESC
    LIMIT :limit_plus_one
    """)

_MESSAGE_DEPTH_QUERY = text("""
    WITH RECURSIVE message_path AS (
        SELECT m.id, m.tenant_id, m.session_id, m.parent_message_id,
               0::bigint AS depth
        FROM chat_message AS m
        WHERE m.tenant_id = CAST(:tenant_id AS uuid)
          AND m.session_id = CAST(:session_id AS uuid)
          AND m.id = CAST(:tip_id AS uuid)
        UNION ALL
        SELECT parent.id, parent.tenant_id, parent.session_id,
               parent.parent_message_id, child.depth + 1
        FROM chat_message AS parent
        JOIN message_path AS child
          ON parent.tenant_id = child.tenant_id
         AND parent.session_id = child.session_id
         AND parent.id = child.parent_message_id
    )
    SELECT depth FROM message_path WHERE id = CAST(:message_id AS uuid)
    """)

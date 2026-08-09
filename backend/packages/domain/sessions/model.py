"""Platform Session records independent from Runtime session state."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from pydantic import JsonValue

SessionStatus = Literal["ACTIVE", "ARCHIVED", "DELETED"]
MessageRole = Literal["USER", "ASSISTANT", "SYSTEM", "TOOL"]
MessageContentType = Literal[
    "text", "artifact_reference", "tool_reference", "error_notice"
]


@dataclass(frozen=True, slots=True)
class MessageContentPartRecord:
    type: MessageContentType
    text: str | None
    artifact_id: str | None
    tool_call_id: str | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class MessageRecord:
    id: UUID
    tenant_id: UUID
    session_id: UUID
    branch_id: UUID | None
    parent_message_id: UUID | None
    role: MessageRole
    content_parts: tuple[MessageContentPartRecord, ...]
    content_schema_version: str
    source_run_id: UUID | None
    created_at: datetime
    created_by: UUID


def parse_message_content_parts(
    value: object,
) -> tuple[MessageContentPartRecord, ...]:
    """Validate the frozen content-part union before it crosses domain boundaries."""

    if not isinstance(value, list) or not value:
        raise ValueError("message content_parts must be a non-empty array")
    parts: list[MessageContentPartRecord] = []
    allowed_keys = {
        "type",
        "text",
        "artifact_id",
        "tool_call_id",
        "error_code",
    }
    raw_parts = cast(list[object], value)
    for raw_part in raw_parts:
        if not isinstance(raw_part, dict):
            raise TypeError("message content part shape is invalid")
        untyped_part = cast(dict[object, object], raw_part)
        if any(
            not isinstance(key, str) or key not in allowed_keys for key in untyped_part
        ):
            raise ValueError("message content part shape is invalid")
        part = cast(dict[str, object], untyped_part)
        part_type = part.get("type")
        if part_type not in {
            "text",
            "artifact_reference",
            "tool_reference",
            "error_notice",
        }:
            raise ValueError("message content part type is invalid")
        values = {
            key: _optional_part_text(part.get(key))
            for key in ("text", "artifact_id", "tool_call_id", "error_code")
        }
        if part_type == "text" and (
            values["text"] is None
            or any(
                values[key] is not None
                for key in ("artifact_id", "tool_call_id", "error_code")
            )
        ):
            raise ValueError("text content part is invalid")
        if part_type == "artifact_reference" and (
            values["artifact_id"] is None
            or any(
                values[key] is not None
                for key in ("text", "tool_call_id", "error_code")
            )
        ):
            raise ValueError("artifact content part is invalid")
        if part_type == "tool_reference" and (
            values["tool_call_id"] is None
            or any(
                values[key] is not None for key in ("text", "artifact_id", "error_code")
            )
        ):
            raise ValueError("tool content part is invalid")
        if part_type == "error_notice" and (
            values["error_code"] is None
            or values["artifact_id"] is not None
            or values["tool_call_id"] is not None
        ):
            raise ValueError("error content part is invalid")
        parts.append(
            MessageContentPartRecord(
                type=cast(MessageContentType, part_type),
                text=values["text"],
                artifact_id=values["artifact_id"],
                tool_call_id=values["tool_call_id"],
                error_code=values["error_code"],
            )
        )
    return tuple(parts)


def _optional_part_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("message content part values must be non-empty strings")
    return value


@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: UUID
    tenant_id: UUID
    user_id: UUID
    agent_id: UUID
    default_deployment_id: UUID
    title: str | None
    status: SessionStatus
    metadata: dict[str, JsonValue]
    metadata_schema_version: str
    resource_version: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    deleted_at: datetime | None

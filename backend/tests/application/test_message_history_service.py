"""Message history authorization, ownership and DTO mapping tests."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from packages.application.public import (
    MessageHistoryService,
    MessageHistoryStore,
    RequestMetadata,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    PlatformError,
    SubjectType,
    TenantContext,
)
from packages.domain.public import (
    MessageContentPartRecord,
    MessageRecord,
    TenantAccess,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
SESSION_ID = UUID("33333333-3333-4333-8333-333333333333")
MESSAGE_ID = UUID("44444444-4444-4444-8444-444444444444")
BRANCH_ID = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2026, 8, 8, tzinfo=UTC)
METADATA = RequestMetadata(request_id="req-message", trace_id="trace-message")


def principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        identity_issuer="https://issuer.test",
        external_subject="message-user",
        display_name="Message User",
        platform_roles=frozenset(),
        auth_time=NOW,
    )


def access(*permissions: str) -> TenantAccess:
    return TenantAccess(
        context=TenantContext(
            tenant_id=str(TENANT_ID),
            subject_type=SubjectType.USER,
            subject_id=str(ACTOR_ID),
            membership_version=1,
            auth_time=NOW,
            request_id=METADATA.request_id,
            trace_id=METADATA.trace_id,
        ),
        permissions=frozenset(permissions),
    )


class ResolverStub:
    def __init__(self, tenant_access: TenantAccess) -> None:
        self.tenant_access = tenant_access

    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess:
        return self.tenant_access


class StoreStub:
    def __init__(self) -> None:
        self.user_id: UUID | None = None
        self.branch_id: UUID | None = None

    async def list_session_messages(self, context: TenantContext, **kwargs: object):
        self.user_id = cast(UUID, kwargs["user_id"])
        self.branch_id = cast(UUID | None, kwargs["branch_id"])
        return (
            [
                MessageRecord(
                    id=MESSAGE_ID,
                    tenant_id=TENANT_ID,
                    session_id=SESSION_ID,
                    branch_id=BRANCH_ID,
                    parent_message_id=None,
                    role="ASSISTANT",
                    content_parts=(
                        MessageContentPartRecord(
                            type="text",
                            text="Hello",
                            artifact_id=None,
                            tool_call_id=None,
                            error_code=None,
                        ),
                    ),
                    content_schema_version="message-content/v1",
                    source_run_id=None,
                    created_at=NOW,
                    created_by=ACTOR_ID,
                )
            ],
            "next-message-cursor",
        )


def service(tenant_access: TenantAccess, store: StoreStub) -> MessageHistoryService:
    return MessageHistoryService(
        ResolverStub(tenant_access), cast(MessageHistoryStore, store)
    )


@pytest.mark.asyncio
async def test_message_history_uses_actor_owner_and_maps_frozen_page() -> None:
    store = StoreStub()
    history = service(access("session:read", "message:list"), store)

    page = await history.list_session_messages(
        principal(),
        session_id=str(SESSION_ID),
        limit=20,
        cursor=None,
        branch_id=str(BRANCH_ID),
        metadata=METADATA,
    )

    assert store.user_id == ACTOR_ID
    assert store.branch_id == BRANCH_ID
    assert page.items[0].content_parts[0].text == "Hello"
    assert page.next_cursor == "next-message-cursor"
    assert page.has_more is True


@pytest.mark.asyncio
@pytest.mark.parametrize("permissions", [("session:read",), ("message:list",), ()])
async def test_message_history_requires_session_and_message_permissions(
    permissions: tuple[str, ...],
) -> None:
    history = service(access(*permissions), StoreStub())

    with pytest.raises(PlatformError) as raised:
        await history.list_session_messages(
            principal(),
            session_id=str(SESSION_ID),
            limit=20,
            cursor=None,
            branch_id=None,
            metadata=METADATA,
        )

    assert raised.value.code == "PERMISSION_DENIED"

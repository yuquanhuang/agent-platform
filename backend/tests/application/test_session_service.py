"""Session application ownership, authorization and mapping tests."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from packages.application.public import (
    RequestMetadata,
    SessionManagementService,
    SessionStore,
)
from packages.contracts.generated.core_models import SessionUpdateRequest
from packages.contracts.public import (
    AuthenticatedPrincipal,
    PlatformError,
    SubjectType,
    TenantContext,
)
from packages.domain.public import SessionRecord, TenantAccess

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
SESSION_ID = UUID("33333333-3333-4333-8333-333333333333")
AGENT_ID = UUID("44444444-4444-4444-8444-444444444444")
DEPLOYMENT_ID = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2026, 8, 8, tzinfo=UTC)
METADATA = RequestMetadata(request_id="req-session", trace_id="trace-session")


def principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        identity_issuer="https://issuer.test",
        external_subject="session-user",
        display_name="Session User",
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


def record() -> SessionRecord:
    return SessionRecord(
        id=SESSION_ID,
        tenant_id=TENANT_ID,
        user_id=ACTOR_ID,
        agent_id=AGENT_ID,
        default_deployment_id=DEPLOYMENT_ID,
        title="Pinned conversation",
        status="ACTIVE",
        metadata={"locale": "zh-CN"},
        metadata_schema_version="session-metadata/v1",
        resource_version=3,
        created_at=NOW,
        updated_at=NOW,
        archived_at=None,
        deleted_at=None,
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

    async def list_sessions(self, context: TenantContext, **kwargs: object):
        self.user_id = cast(UUID, kwargs["user_id"])
        return [record()], None

    async def get_session(self, context: TenantContext, **kwargs: object):
        self.user_id = cast(UUID, kwargs["user_id"])
        return record()


def service(tenant_access: TenantAccess, store: StoreStub) -> SessionManagementService:
    return SessionManagementService(
        ResolverStub(tenant_access), cast(SessionStore, store)
    )


@pytest.mark.asyncio
async def test_list_sessions_always_uses_authenticated_actor_as_owner() -> None:
    store = StoreStub()
    session_service = service(access("session:list"), store)

    page = await session_service.list_sessions(
        principal(),
        limit=20,
        cursor=None,
        agent_id=None,
        status=None,
        metadata=METADATA,
    )

    assert store.user_id == ACTOR_ID
    assert page.items[0].default_deployment_id == str(DEPLOYMENT_ID)
    assert page.has_more is False


@pytest.mark.asyncio
async def test_get_session_returns_strong_etag_without_runtime_state() -> None:
    store = StoreStub()
    session_service = service(access("session:read"), store)

    result, etag = await session_service.get_session(
        principal(), session_id=str(SESSION_ID), metadata=METADATA
    )

    assert store.user_id == ACTOR_ID
    assert result.status == "ACTIVE"
    assert etag == '"rv:3"'


@pytest.mark.asyncio
async def test_session_update_rejects_empty_body_before_persistence() -> None:
    store = StoreStub()
    session_service = service(access("session:update"), store)

    with pytest.raises(PlatformError) as raised:
        await session_service.update_session(
            principal(),
            session_id=str(SESSION_ID),
            if_match='"rv:3"',
            request=SessionUpdateRequest.model_validate({}),
            metadata=METADATA,
        )

    assert raised.value.code == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_session_read_requires_explicit_permission() -> None:
    store = StoreStub()
    session_service = service(access("session:list"), store)

    with pytest.raises(PlatformError) as raised:
        await session_service.get_session(
            principal(), session_id=str(SESSION_ID), metadata=METADATA
        )

    assert raised.value.code == "PERMISSION_DENIED"

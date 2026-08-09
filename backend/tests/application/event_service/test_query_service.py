"""RunEvent history authorization, replay integrity and sensitive-data tests."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from pydantic import JsonValue

from packages.application.event_service import (
    RunEventPageRecord,
    RunEventQueryService,
    RunEventQueryStore,
    RunEventRecord,
)
from packages.application.public import RequestMetadata
from packages.contracts.public import (
    AuthenticatedPrincipal,
    PlatformError,
    SubjectType,
    TenantContext,
)
from packages.domain.public import TenantAccess

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
SESSION_ID = UUID("33333333-3333-4333-8333-333333333333")
RUN_ID = UUID("44444444-4444-4444-8444-444444444444")
MESSAGE_ID = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2026, 8, 8, tzinfo=UTC)
METADATA = RequestMetadata(request_id="req-event-query", trace_id="trace-event-query")


def principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        identity_issuer="https://issuer.test",
        external_subject="event-reader",
        display_name="Event Reader",
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


def event(sequence_no: int, event_type: str = "run_created") -> RunEventRecord:
    payload: dict[str, JsonValue]
    if event_type == "thinking_delta":
        payload = {
            "message_id": str(MESSAGE_ID),
            "delta": "private reasoning",
            "visibility": "debug_only",
        }
    else:
        payload = {
            "deployment_id": "66666666-6666-4666-8666-666666666666",
            "snapshot_id": "77777777-7777-4777-8777-777777777777",
        }
    return RunEventRecord(
        id=UUID(int=sequence_no),
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        session_id=SESSION_ID,
        sequence_no=sequence_no,
        source_event_id=f"source-{sequence_no}",
        execution_attempt=1,
        schema_version="1.0",
        event_type=event_type,
        payload_version="1.0",
        payload=payload,
        occurred_at=NOW,
        recorded_at=NOW,
        trace_id="trace-event-query",
    )


class ResolverStub:
    def __init__(self, tenant_access: TenantAccess) -> None:
        self.tenant_access = tenant_access

    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess:
        del principal, metadata
        return self.tenant_access


class StoreStub:
    def __init__(self, page: RunEventPageRecord | None) -> None:
        self.page = page
        self.user_id: UUID | None = None
        self.after: int | None = None
        self.limit: int | None = None

    async def list_events(self, context: TenantContext, **kwargs: object):
        del context
        self.user_id = cast(UUID, kwargs["user_id"])
        self.after = cast(int, kwargs["after"])
        self.limit = cast(int, kwargs["limit"])
        return self.page


def service(tenant_access: TenantAccess, store: StoreStub) -> RunEventQueryService:
    return RunEventQueryService(
        ResolverStub(tenant_access), cast(RunEventQueryStore, store)
    )


@pytest.mark.asyncio
async def test_query_maps_owned_contiguous_page_and_cursor() -> None:
    store = StoreStub(
        RunEventPageRecord(
            events=(event(2), event(3)), latest_sequence_no=4, has_more=True
        )
    )

    page = await service(access("run:read"), store).list_events(
        principal(),
        run_id=str(RUN_ID),
        after=1,
        limit=2,
        metadata=METADATA,
    )

    assert store.user_id == ACTOR_ID
    assert store.after == 1
    assert store.limit == 2
    assert [item.sequence_no for item in page.items] == [2, 3]
    assert page.latest_sequence_no == 4
    assert page.has_more is True


@pytest.mark.asyncio
async def test_query_redacts_thinking_without_sensitive_permission() -> None:
    store = StoreStub(
        RunEventPageRecord(
            events=(event(1, "thinking_delta"),),
            latest_sequence_no=1,
            has_more=False,
        )
    )

    page = await service(access("run:read"), store).list_events(
        principal(), run_id=str(RUN_ID), after=0, limit=20, metadata=METADATA
    )

    assert page.items[0].event_type == "thinking_delta"
    assert page.items[0].payload.delta == "Sensitive thinking content was redacted."


@pytest.mark.asyncio
async def test_query_preserves_thinking_with_sensitive_permission() -> None:
    store = StoreStub(
        RunEventPageRecord(
            events=(event(1, "thinking_delta"),),
            latest_sequence_no=1,
            has_more=False,
        )
    )

    page = await service(access("run:read", "run:view_sensitive"), store).list_events(
        principal(), run_id=str(RUN_ID), after=0, limit=20, metadata=METADATA
    )

    assert page.items[0].event_type == "thinking_delta"
    assert page.items[0].payload.delta == "private reasoning"


@pytest.mark.asyncio
async def test_query_returns_empty_page_at_or_beyond_latest_sequence() -> None:
    page_record = RunEventPageRecord(events=(), latest_sequence_no=2, has_more=False)

    page = await service(access("run:read"), StoreStub(page_record)).list_events(
        principal(), run_id=str(RUN_ID), after=3, limit=20, metadata=METADATA
    )

    assert page.items == []
    assert page.latest_sequence_no == 2
    assert page.has_more is False


@pytest.mark.asyncio
async def test_query_rejects_sequence_gap_with_stable_details() -> None:
    store = StoreStub(
        RunEventPageRecord(
            events=(event(1), event(3)), latest_sequence_no=3, has_more=False
        )
    )

    with pytest.raises(PlatformError) as raised:
        await service(access("run:read"), store).list_events(
            principal(), run_id=str(RUN_ID), after=0, limit=20, metadata=METADATA
        )

    assert raised.value.status_code == 409
    assert raised.value.code == "RUN_EVENT_SEQUENCE_GAP"
    assert raised.value.details == {
        "after": 0,
        "expected_sequence_no": 2,
        "observed_sequence_no": 3,
        "latest_sequence_no": 3,
    }


@pytest.mark.asyncio
async def test_query_hides_unavailable_run_and_requires_read_permission() -> None:
    with pytest.raises(PlatformError) as missing:
        await service(access("run:read"), StoreStub(None)).list_events(
            principal(), run_id=str(RUN_ID), after=0, limit=20, metadata=METADATA
        )
    assert missing.value.code == "RESOURCE_NOT_FOUND"

    with pytest.raises(PlatformError) as denied:
        await service(access(), StoreStub(None)).list_events(
            principal(), run_id=str(RUN_ID), after=0, limit=20, metadata=METADATA
        )
    assert denied.value.code == "PERMISSION_DENIED"

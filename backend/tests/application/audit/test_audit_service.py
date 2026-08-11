"""Audit query authorization, pagination and projection tests."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from packages.application.audit import AuditManagementService
from packages.application.metadata import RequestMetadata
from packages.contracts.public import (
    AuthenticatedPrincipal,
    PlatformError,
    SubjectType,
    TenantContext,
)
from packages.domain.public import AuditLogRecord, TenantAccess

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
RUN_ID = UUID("33333333-3333-4333-8333-333333333333")
EVENT_ID = UUID("44444444-4444-4444-8444-444444444444")
NOW = datetime(2026, 8, 10, tzinfo=UTC)


class Stub:
    def __init__(self, permissions: set[str]) -> None:
        self.permissions = permissions
        self.calls: list[dict[str, object]] = []

    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess:
        del principal
        return TenantAccess(
            context=TenantContext(
                tenant_id=str(TENANT_ID),
                subject_type=SubjectType.USER,
                subject_id=str(ACTOR_ID),
                membership_version=1,
                auth_time=NOW,
                request_id=metadata.request_id,
                trace_id=metadata.trace_id,
            ),
            permissions=frozenset(self.permissions),
        )

    async def list_audit_logs(self, context: TenantContext, **kwargs: object):
        self.calls.append(kwargs)
        return [
            AuditLogRecord(
                event_id=EVENT_ID,
                tenant_id=TENANT_ID,
                occurred_at=NOW,
                actor_type="service",
                actor_id=ACTOR_ID,
                action="tool.execute",
                resource_type="tool",
                resource_id=None,
                run_id=RUN_ID,
                result="SUCCESS",
                trace_id="trace-audit",
            )
        ], None


def principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        identity_issuer="https://issuer.test",
        external_subject="auditor",
        display_name="Auditor",
        active_tenant_id=str(TENANT_ID),
        membership_version=1,
        auth_time=NOW,
    )


@pytest.mark.asyncio
async def test_audit_query_requires_audit_list_permission_and_redacts_projection() -> (
    None
):
    denied_stub = Stub(set())
    denied_service = AuditManagementService(denied_stub, denied_stub)
    with pytest.raises(PlatformError) as denied:
        await denied_service.list_audit_logs(
            principal(),
            action=None,
            resource_type=None,
            actor_id=None,
            run_id=None,
            occurred_from=None,
            occurred_to=None,
            limit=20,
            cursor=None,
            metadata=RequestMetadata(request_id="req-audit", trace_id="trace-audit"),
        )
    assert denied.value.code == "PERMISSION_DENIED"
    assert denied_stub.calls == []

    stub = Stub({"audit:list"})
    service = AuditManagementService(stub, stub)
    page = await service.list_audit_logs(
        principal(),
        action="tool.execute",
        resource_type="tool",
        actor_id=str(ACTOR_ID),
        run_id=str(RUN_ID),
        occurred_from=NOW,
        occurred_to=NOW,
        limit=20,
        cursor=None,
        metadata=RequestMetadata(request_id="req-audit", trace_id="trace-audit"),
    )
    assert page.items[0].event_id == str(EVENT_ID)
    assert page.items[0].resource_id == ""
    assert not hasattr(page.items[0], "metadata_json")
    assert stub.calls[0]["run_id"] == RUN_ID


@pytest.mark.asyncio
async def test_audit_query_rejects_reversed_time_range() -> None:
    stub = Stub({"audit:list"})
    service = AuditManagementService(stub, stub)
    with pytest.raises(PlatformError) as invalid:
        await service.list_audit_logs(
            principal(),
            action=None,
            resource_type=None,
            actor_id=None,
            run_id=None,
            occurred_from=NOW,
            occurred_to=NOW.replace(day=9),
            limit=20,
            cursor=None,
            metadata=RequestMetadata(request_id="req-audit", trace_id="trace-audit"),
        )
    assert invalid.value.code == "VALIDATION_ERROR"

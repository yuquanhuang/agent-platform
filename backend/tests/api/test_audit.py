"""Frozen Audit route registration and response tests."""

import json
import re
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from apps.api.app import create_app
from packages.application.audit import AuditManagementService
from packages.application.metadata import RequestMetadata
from packages.contracts.public import AuthenticatedPrincipal, SubjectType, TenantContext
from packages.domain.public import AuditLogRecord, TenantAccess
from packages.infrastructure.auth.public import MockIdentityProvider
from packages.infrastructure.public import AppSettings

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")


class Stub:
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
                auth_time=datetime(2026, 8, 10, tzinfo=UTC),
                request_id=metadata.request_id,
                trace_id=metadata.trace_id,
            ),
            permissions=frozenset({"audit:list"}),
        )

    async def list_audit_logs(self, context: TenantContext, **kwargs: object):
        return [
            AuditLogRecord(
                event_id=UUID("33333333-3333-4333-8333-333333333333"),
                tenant_id=TENANT_ID,
                occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
                actor_type="user",
                actor_id=ACTOR_ID,
                action="run.create",
                resource_type="run",
                resource_id=UUID("44444444-4444-4444-8444-444444444444"),
                run_id=UUID("44444444-4444-4444-8444-444444444444"),
                result="SUCCESS",
                trace_id="trace-audit",
            )
        ], None


def build_app() -> FastAPI:
    settings = AppSettings(
        mock_active_tenant_id=str(TENANT_ID), mock_membership_version=1
    )
    return create_app(
        settings,
        identity_provider=MockIdentityProvider(settings),
        audit_service=AuditManagementService(Stub(), Stub()),
    )


def test_app_openapi_registers_frozen_audit_operation() -> None:
    app = build_app()
    operation_ids = set(
        re.findall(r'"operationId":\s*"([^"]+)"', json.dumps(app.openapi()))
    )
    assert "listAuditLogs" in operation_ids


@pytest.mark.asyncio
async def test_list_audit_logs_returns_minimal_projection() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/api/v1/audit-logs",
            headers={"Authorization": "Bearer mock"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["action"] == "run.create"
    assert "metadata_json" not in body["items"][0]

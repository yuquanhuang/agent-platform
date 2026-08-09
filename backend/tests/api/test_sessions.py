"""Session route registration, response shape and ETag tests."""

import json
import re
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import httpx
import pytest

from apps.api.app import create_app
from packages.application.public import (
    MessageHistoryService,
    MessageHistoryStore,
    RequestMetadata,
    SessionManagementService,
    SessionStore,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    SubjectType,
    TenantContext,
)
from packages.domain.public import (
    MessageContentPartRecord,
    MessageRecord,
    SessionRecord,
    TenantAccess,
)
from packages.infrastructure.auth.public import MockIdentityProvider
from packages.infrastructure.public import AppSettings

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
SESSION_ID = UUID("33333333-3333-4333-8333-333333333333")
AGENT_ID = UUID("44444444-4444-4444-8444-444444444444")
DEPLOYMENT_ID = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2026, 8, 8, tzinfo=UTC)


class SessionStub:
    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess:
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
            permissions=frozenset({"session:read", "message:list"}),
        )

    async def get_session(self, context: TenantContext, **kwargs: object):
        return SessionRecord(
            id=SESSION_ID,
            tenant_id=TENANT_ID,
            user_id=ACTOR_ID,
            agent_id=AGENT_ID,
            default_deployment_id=DEPLOYMENT_ID,
            title="Pinned conversation",
            status="ACTIVE",
            metadata={},
            metadata_schema_version="session-metadata/v1",
            resource_version=4,
            created_at=NOW,
            updated_at=NOW,
            archived_at=None,
            deleted_at=None,
        )

    async def list_session_messages(self, context: TenantContext, **kwargs: object):
        return (
            [
                MessageRecord(
                    id=UUID("66666666-6666-4666-8666-666666666666"),
                    tenant_id=TENANT_ID,
                    session_id=SESSION_ID,
                    branch_id=None,
                    parent_message_id=None,
                    role="USER",
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
            None,
        )


def build_app():
    settings = AppSettings()
    provider = MockIdentityProvider(settings, now=lambda: NOW)
    stub = SessionStub()
    service = SessionManagementService(stub, cast(SessionStore, stub))
    message_service = MessageHistoryService(stub, cast(MessageHistoryStore, stub))
    return create_app(
        settings,
        identity_provider=provider,
        session_service=service,
        message_history_service=message_service,
    )


@pytest.mark.asyncio
async def test_get_session_returns_frozen_shape_and_strong_etag() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(
            f"/api/v1/sessions/{SESSION_ID}",
            headers={"Authorization": "Bearer mock", "X-Request-ID": "req-session"},
        )

    assert response.status_code == 200
    assert response.headers["ETag"] == '"rv:4"'
    assert response.json()["default_deployment_id"] == str(DEPLOYMENT_ID)
    assert "metadata" not in response.json()


def test_app_openapi_registers_all_frozen_session_operations() -> None:
    operation_ids: set[str] = set(
        re.findall(r'"operationId":\s*"([^"]+)"', json.dumps(build_app().openapi()))
    )

    assert {
        "listSessions",
        "createSession",
        "getSession",
        "updateSession",
        "archiveSession",
        "deleteSession",
        "listSessionMessages",
    } <= operation_ids


@pytest.mark.asyncio
async def test_list_session_messages_returns_read_only_history_page() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(
            f"/api/v1/sessions/{SESSION_ID}/messages",
            headers={"Authorization": "Bearer mock", "X-Request-ID": "req-message"},
        )

    assert response.status_code == 200
    assert response.json()["items"][0]["content_parts"] == [
        {
            "type": "text",
            "text": "Hello",
            "artifact_id": None,
            "tool_call_id": None,
            "error_code": None,
        }
    ]

"""Frozen Approval route registration and response tests."""

import json
import re
from typing import cast

import httpx
import pytest

from apps.api.app import create_app
from packages.application.approvals import ApprovalManagementService, ApprovalStore
from packages.contracts.generated.core_models import ApprovalDecisionRequest
from packages.infrastructure.auth.public import MockIdentityProvider
from packages.infrastructure.public import AppSettings
from tests.application.approvals.test_approval_service import (
    APPROVAL_ID,
    Stub,
)


def build_app(actor_stub: Stub | None = None):
    settings = AppSettings()
    stub = actor_stub or Stub()
    app = create_app(
        settings,
        identity_provider=MockIdentityProvider(settings),
        approval_service=ApprovalManagementService(
            stub, cast(ApprovalStore, stub), workflow_control=stub
        ),
    )
    app.state.approval_stub = stub
    return app


def test_app_openapi_registers_frozen_approval_operations() -> None:
    operation_ids = set(
        re.findall(r'"operationId":\s*"([^"]+)"', json.dumps(build_app().openapi()))
    )
    assert {"listApprovals", "getApproval", "decideApproval"} <= operation_ids


@pytest.mark.asyncio
async def test_get_approval_returns_strong_etag() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(
            f"/api/v1/approvals/{APPROVAL_ID}",
            headers={"Authorization": "Bearer mock"},
        )

    assert response.status_code == 200
    assert response.headers["etag"] == '"rv:1"'
    assert response.json()["status"] == "PENDING"


@pytest.mark.asyncio
async def test_decide_approval_requires_frozen_headers_and_returns_new_etag() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/v1/approvals/{APPROVAL_ID}/decision",
            headers={
                "Authorization": "Bearer mock",
                "Idempotency-Key": "approval-key-004",
                "If-Match": '"rv:1"',
            },
            json=ApprovalDecisionRequest(decision="REJECTED", comment=None).model_dump(
                mode="json"
            ),
        )

    assert response.status_code == 200
    assert response.headers["etag"] == '"rv:2"'
    assert response.json()["status"] == "REJECTED"


@pytest.mark.asyncio
async def test_decide_approval_rejects_missing_concurrency_headers() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/v1/approvals/{APPROVAL_ID}/decision",
            headers={"Authorization": "Bearer mock"},
            json={"decision": "APPROVED"},
        )

    assert response.status_code == 422

"""Generated contract and client integration tests."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from packages.contracts.generated.core_client import (
    OPERATION_IDS as CORE_OPERATION_IDS,
)
from packages.contracts.generated.core_client import CoreApiClient
from packages.contracts.generated.resources_client import (
    OPERATION_IDS as RESOURCE_OPERATION_IDS,
)
from packages.contracts.generated.run_event import (
    RUN_EVENT_ADAPTER,
    TextDeltaEvent,
)
from packages.contracts.generated.transport import AsyncApiTransport

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_generated_run_event_accepts_golden_example() -> None:
    payload = json.loads(
        (
            PROJECT_ROOT
            / "docs"
            / "agent-platform"
            / "examples"
            / "run-event-text-delta-v1.example.json"
        ).read_text(encoding="utf-8")
    )

    event = RUN_EVENT_ADAPTER.validate_python(payload)

    assert isinstance(event, TextDeltaEvent)
    assert event.sequence_no == 42
    assert event.payload.delta == "分析结果如下："


def test_generated_run_event_rejects_payload_for_other_discriminator() -> None:
    payload = json.loads(
        (
            PROJECT_ROOT
            / "docs"
            / "agent-platform"
            / "examples"
            / "run-event-text-delta-v1.example.json"
        ).read_text(encoding="utf-8")
    )
    payload["event_type"] = "run_failed"

    with pytest.raises(ValidationError):
        RUN_EVENT_ADAPTER.validate_python(payload)


def test_generated_operation_catalog_covers_both_frozen_openapi_files() -> None:
    assert len(CORE_OPERATION_IDS) == 40
    assert len(RESOURCE_OPERATION_IDS) == 127
    assert CORE_OPERATION_IDS.isdisjoint(RESOURCE_OPERATION_IDS)


@pytest.mark.asyncio
async def test_generated_core_client_uses_typed_transport() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/me"
        return httpx.Response(
            200,
            json={
                "user_id": "user_01",
                "external_subject": "mock-user-01",
                "display_name": "Mock User",
                "active_tenant_id": None,
                "memberships": [],
                "auth_time": "2026-08-05T08:00:00Z",
            },
        )

    async with httpx.AsyncClient(
        base_url="https://agent-platform.test",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = CoreApiClient(AsyncApiTransport(http_client))

        identity = await client.get_current_identity()

    assert identity.user_id == "user_01"
    assert identity.active_tenant_id is None

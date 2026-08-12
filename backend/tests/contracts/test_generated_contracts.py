"""Generated contract and client integration tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import yaml
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from packages.contracts.generated.core_client import (
    OPERATION_IDS as CORE_OPERATION_IDS,
)
from packages.contracts.generated.core_client import CoreApiClient
from packages.contracts.generated.core_models import AgentUpdateRequest
from packages.contracts.generated.resources_client import (
    OPERATION_IDS as RESOURCE_OPERATION_IDS,
)
from packages.contracts.generated.run_event import (
    RUN_EVENT_ADAPTER,
    TextDeltaEvent,
)
from packages.contracts.generated.transport import AsyncApiTransport

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _agent_binding_validator() -> Draft202012Validator:
    document = cast(
        dict[str, Any],
        yaml.safe_load(
            (
                PROJECT_ROOT
                / "docs"
                / "agent-platform"
                / "agent-platform-openapi-v1.yaml"
            ).read_text(encoding="utf-8")
        ),
    )
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "components": document["components"],
        "$ref": "#/components/schemas/AgentBindingList",
    }
    return Draft202012Validator(schema)


def _agent_binding_errors(bindings: list[dict[str, Any]]) -> list[Any]:
    validator = cast(Any, _agent_binding_validator())
    return list(validator.iter_errors(bindings))


def _model_binding(
    suffix: str,
    *,
    role: str | None = None,
    configure_fallback: bool = False,
    error_codes: list[str] | None = None,
) -> dict[str, Any]:
    binding: dict[str, Any] = {
        "resource_type": "model",
        "resource_id": f"model_{suffix}",
        "version_policy": "fixed",
        "version_id": f"model_version_{suffix}",
    }
    if role is not None:
        binding["binding_role"] = role
    if configure_fallback:
        binding["configuration_schema_version"] = "model-routing/v1"
        binding["configuration"] = {
            "fallback_error_codes": error_codes or ["RATE_LIMITED"]
        }
    return binding


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
    assert len(CORE_OPERATION_IDS) == 42
    assert len(RESOURCE_OPERATION_IDS) == 134
    assert "createQuotaPolicy" in RESOURCE_OPERATION_IDS
    assert "listQuotaPolicyVersions" in RESOURCE_OPERATION_IDS
    assert CORE_OPERATION_IDS.isdisjoint(RESOURCE_OPERATION_IDS)


def test_generated_agent_binding_types_keep_legacy_single_model_compatible() -> None:
    request = AgentUpdateRequest.model_validate(
        {"bindings": [_model_binding("primary")]}
    )

    assert request.bindings is not None
    assert request.bindings[0].binding_role is None


@pytest.mark.parametrize(
    "bindings",
    [
        [_model_binding("legacy")],
        [
            _model_binding("primary", role="primary", configure_fallback=True),
            _model_binding("fallback", role="fallback_1"),
        ],
        [
            _model_binding("primary", role="primary", configure_fallback=True),
            _model_binding("fallback_1", role="fallback_1"),
            _model_binding("fallback_2", role="fallback_2"),
        ],
    ],
)
def test_frozen_agent_binding_schema_accepts_supported_model_routes(
    bindings: list[dict[str, Any]],
) -> None:
    assert _agent_binding_errors(bindings) == []


@pytest.mark.parametrize(
    "bindings",
    [
        [_model_binding("one"), _model_binding("two")],
        [
            _model_binding("one", role="primary", configure_fallback=True),
            _model_binding("two", role="primary"),
        ],
        [
            _model_binding("primary", role="primary", configure_fallback=True),
            _model_binding("fallback_2", role="fallback_2"),
        ],
        [
            _model_binding("primary", role="primary", configure_fallback=True),
            _model_binding("fallback_1", role="fallback_1"),
            _model_binding("fallback_2", role="fallback_2"),
            _model_binding("extra", role="fallback_2"),
        ],
        [
            _model_binding(
                "primary",
                role="primary",
                configure_fallback=True,
                error_codes=["PROVIDER_TIMEOUT"],
            ),
            _model_binding("fallback", role="fallback_1"),
        ],
        [
            {
                "resource_type": "prompt",
                "resource_id": "prompt_1",
                "version_policy": "fixed",
                "version_id": "prompt_version_1",
                "binding_role": "primary",
            }
        ],
    ],
)
def test_frozen_agent_binding_schema_rejects_unsafe_model_routes(
    bindings: list[dict[str, Any]],
) -> None:
    assert _agent_binding_errors(bindings)


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

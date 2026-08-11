"""Deterministic JSON Schema catalog generated from internal Pydantic models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from packages.contracts.sandbox_api.models import (
    SandboxActionResponse,
    SandboxDetailResponse,
    SandboxInspectResponse,
    SandboxLeaseRequest,
    SandboxLeaseResponse,
    SandboxOperationAccepted,
    SandboxProcessActionResponse,
    SandboxProcessControlRequest,
    SandboxProcessRequest,
    SandboxProcessResponse,
    SandboxProvisionRequest,
    SandboxReleaseRequest,
)


def sandbox_api_schema_catalog() -> dict[str, Any]:
    """Return endpoint schemas without introducing a second hand-written contract."""

    return {
        "schema_version": "1.0",
        "operations": {
            "provisionSandbox": _operation(
                request=SandboxProvisionRequest,
                response=SandboxOperationAccepted,
            ),
            "getSandbox": _operation(response=SandboxDetailResponse),
            "inspectSandbox": _operation(response=SandboxInspectResponse),
            "acquireSandboxLease": _operation(
                request=SandboxLeaseRequest,
                response=SandboxLeaseResponse,
            ),
            "startSandboxProcess": _operation(
                request=SandboxProcessRequest,
                response=SandboxProcessResponse,
            ),
            "cancelSandboxProcess": _operation(
                request=SandboxProcessControlRequest,
                response=SandboxProcessActionResponse,
            ),
            "terminateSandbox": _operation(response=SandboxActionResponse),
            "releaseSandbox": _operation(
                request=SandboxReleaseRequest, response=SandboxActionResponse
            ),
            "destroySandbox": _operation(response=SandboxActionResponse),
        },
    }


def _operation(
    *,
    response: type[BaseModel],
    request: type[BaseModel] | None = None,
) -> dict[str, object]:
    request_schema = request.model_json_schema(mode="validation") if request else None
    response_schema = response.model_json_schema(mode="serialization")
    return {"request": request_schema, "response": response_schema}

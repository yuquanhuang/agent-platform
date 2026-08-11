"""Frozen Sandbox Manager request, response, and schema catalog tests."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel, SecretStr, ValidationError

from packages.contracts.sandbox_api import (
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
    sandbox_api_schema_catalog,
)

HASH = "sha256:" + "a" * 64
OperationModels = tuple[type[BaseModel] | None, type[BaseModel]]
OPERATION_MODELS: dict[str, OperationModels] = {
    "provisionSandbox": (SandboxProvisionRequest, SandboxOperationAccepted),
    "getSandbox": (None, SandboxDetailResponse),
    "inspectSandbox": (None, SandboxInspectResponse),
    "acquireSandboxLease": (SandboxLeaseRequest, SandboxLeaseResponse),
    "startSandboxProcess": (SandboxProcessRequest, SandboxProcessResponse),
    "cancelSandboxProcess": (
        SandboxProcessControlRequest,
        SandboxProcessActionResponse,
    ),
    "terminateSandbox": (None, SandboxActionResponse),
    "releaseSandbox": (SandboxReleaseRequest, SandboxActionResponse),
    "destroySandbox": (None, SandboxActionResponse),
}


def provision_payload() -> dict[str, object]:
    return {
        "tenant_id": "ten_001",
        "user_id": "usr_001",
        "session_id": "ses_001",
        "run_id": "run_001",
        "execution_attempt": 1,
        "scope": "run",
        "policy_ref": "immutable://sandbox-policy/policy_001",
        "policy_hash": HASH,
        "bundle_ref": "bundle://tenant/ten_001/bundle_001",
        "bundle_hash": HASH,
        "workspace_uri": "workspace://tenant/ten_001/runs/run_001/",
        "provision_token": "one-time-token-value",
        "trace_id": "trace_001",
    }


def test_internal_models_are_strict_frozen_and_redact_secrets() -> None:
    request = SandboxProvisionRequest.model_validate(provision_payload())

    assert "one-time-token-value" not in repr(request)
    assert "one-time-token-value" not in request.model_dump_json()
    with pytest.raises(ValidationError):
        SandboxProvisionRequest.model_validate(
            {**provision_payload(), "provider_extension": "not-allowed"}
        )
    with pytest.raises(ValidationError):
        request.run_id = "run_changed"


def test_status_url_must_reference_the_returned_operation() -> None:
    accepted = SandboxOperationAccepted(
        sandbox_id="sbx_001",
        operation_id="op_001",
        status_url="/api/v1/operations/op_001",
    )

    assert accepted.status == "ACCEPTED"
    with pytest.raises(ValidationError, match="accepted operation"):
        SandboxOperationAccepted(
            sandbox_id="sbx_001",
            operation_id="op_001",
            status_url="/api/v1/operations/op_other",
        )


@pytest.mark.parametrize("argument", ["", "hello\x00world", "hello\nworld"])
def test_process_argv_rejects_empty_nul_and_control_characters(argument: str) -> None:
    with pytest.raises(ValidationError):
        SandboxProcessRequest(
            run_id="run_001",
            execution_attempt=1,
            execution_fencing_token=SecretStr("execution-fencing-token"),
            process_id="proc_001",
            argv=[argument],
            working_directory="workspace://tenant/ten_001/runs/run_001/work/",
            timeout_seconds=60,
            trace_id="trace_001",
        )


def test_lease_requires_utc_fencing_window_inputs_and_redacts_token() -> None:
    request = SandboxLeaseRequest.model_validate(
        {
            "run_id": "run_001",
            "execution_attempt": 1,
            "execution_fencing_token": "execution-fencing-token",
            "ttl_seconds": 300,
            "trace_id": "trace_001",
        }
    )

    assert "execution-fencing-token" not in repr(request)
    assert datetime.now(UTC).utcoffset() is not None


def test_schema_catalog_matches_frozen_golden_hash_and_operation_set() -> None:
    catalog = sandbox_api_schema_catalog()
    canonical = json.dumps(catalog, sort_keys=True, separators=(",", ":"))
    actual_hash = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    golden_path = (
        Path(__file__).parents[1] / "golden" / "sandbox_api" / "v1" / "contracts.json"
    )
    golden = cast(dict[str, Any], json.loads(golden_path.read_text()))

    assert actual_hash == golden["schema_catalog_hash"]
    examples = cast(dict[str, dict[str, object]], golden["operation_examples"])
    assert set(catalog["operations"]) == set(examples) == set(OPERATION_MODELS)
    for operation in catalog["operations"].values():
        assert operation["response"]["additionalProperties"] is False
        if operation["request"] is not None:
            assert operation["request"]["additionalProperties"] is False
    for operation_id, (request_model, response_model) in OPERATION_MODELS.items():
        example = examples[operation_id]
        if request_model is None:
            assert example["request"] is None
        else:
            request_model.model_validate(example["request"])
        response_model.model_validate(example["response"])

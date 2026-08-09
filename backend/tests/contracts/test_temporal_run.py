"""Versioned Agent Run Temporal payload validation."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from packages.contracts.temporal import (
    AgentRunWorkflowInput,
    AssistantTextPart,
    CancelAgentRuntimeInput,
    CancelRunSignal,
    InspectAgentRuntimeInput,
    RuntimeCompletion,
    RuntimeInspection,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
RUN_ID = UUID("22222222-2222-4222-8222-222222222222")


def test_run_workflow_input_is_versioned_and_rejects_unknown_fields() -> None:
    input = AgentRunWorkflowInput(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        request_id="req-run",
        trace_id="trace-run",
    )

    assert input.workflow_contract_version == "1.0"
    assert input.initial_execution_attempt == 1
    with pytest.raises(ValidationError):
        AgentRunWorkflowInput.model_validate(
            {**input.model_dump(mode="json"), "prompt": "must-not-enter-history"}
        )


def test_runtime_completion_requires_content_only_for_success() -> None:
    completion = RuntimeCompletion(
        status="SUCCEEDED",
        assistant_content_parts=(AssistantTextPart(text="done"),),
        result_quality="NORMAL",
    )

    assert completion.assistant_content_parts[0].type == "text"
    with pytest.raises(ValidationError, match="requires assistant content"):
        RuntimeCompletion(status="SUCCEEDED")
    with pytest.raises(ValidationError, match="cannot contain assistant content"):
        RuntimeCompletion(
            status="FAILED",
            assistant_content_parts=(AssistantTextPart(text="invalid"),),
            error_code="RUNTIME_FAILED",
            error_message="failed",
        )


def test_cancel_signal_is_small_and_strict() -> None:
    signal = CancelRunSignal(
        signal_id="cancel-1",
        requested_by=RUN_ID,
        requested_at=datetime(2026, 8, 8, tzinfo=UTC),
        reason="user request",
    )

    assert signal.signal_id == "cancel-1"
    with pytest.raises(ValidationError):
        CancelRunSignal.model_validate(
            {**signal.model_dump(mode="json"), "tool_arguments": {"unsafe": True}}
        )


def test_runtime_inspection_requires_safe_terminal_shape() -> None:
    inspection = RuntimeInspection(
        run_id=RUN_ID,
        execution_attempt=1,
        status="LOST",
        safe_to_retry=True,
    )
    assert inspection.workflow_contract_version == "1.1"
    with pytest.raises(ValidationError):
        RuntimeInspection(
            run_id=RUN_ID,
            execution_attempt=1,
            status="UNKNOWN",
            safe_to_retry=True,
        )


def test_runtime_control_inputs_do_not_carry_plaintext_fencing_tokens() -> None:
    inspect = InspectAgentRuntimeInput(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        execution_attempt=1,
        request_id="req-inspect",
        trace_id="trace-inspect",
    )
    cancel = CancelAgentRuntimeInput(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        execution_attempt=1,
        request_id="req-cancel",
        trace_id="trace-cancel",
    )
    assert "fencing_token" not in inspect.model_dump()
    assert "fencing_token" not in cancel.model_dump()

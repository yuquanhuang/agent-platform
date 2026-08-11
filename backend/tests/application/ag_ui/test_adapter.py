"""RunEvent-to-AG-UI v0.1.19 mapping and snapshot tests."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue

from packages.application.ag_ui import (
    AG_UI_EVENT_ADAPTER,
    RunEventAgUiAdapter,
    serialize_ag_ui_event,
)
from packages.application.event_service.service import REDACTED_THINKING_DELTA
from packages.contracts.generated.run_event import RUN_EVENT_ADAPTER, RunEvent

NOW = datetime(2026, 8, 9, tzinfo=UTC)
GOLDEN = (
    Path(__file__).resolve().parents[2]
    / "golden"
    / "ag_ui"
    / "v1"
    / "run_event_mapping.json"
)

PAYLOADS: dict[str, dict[str, JsonValue]] = {
    "run_created": {"deployment_id": "deployment-1", "snapshot_id": "snapshot-1"},
    "run_queued": {"queue": "run-queue", "queued_at": "2026-08-09T00:00:00Z"},
    "run_started": {
        "runtime_type": "agentscope",
        "runtime_target_id": "runtime-target-1",
    },
    "text_message_start": {"message_id": "message-1", "role": "assistant"},
    "text_delta": {"message_id": "message-1", "delta": "hello"},
    "text_message_end": {"message_id": "message-1", "finish_reason": "stop"},
    "thinking_delta": {
        "message_id": "thinking-1",
        "delta": REDACTED_THINKING_DELTA,
        "visibility": "debug_only",
    },
    "plan_updated": {
        "plan_id": "plan-1",
        "steps": [{"step_id": "step-1", "title": "Inspect", "status": "completed"}],
    },
    "tool_call_start": {
        "tool_call_id": "tool-call-1",
        "tool_name": "search",
        "arguments_summary": "query metadata",
    },
    "tool_call_args": {
        "tool_call_id": "tool-call-1",
        "arguments_patch": '{"query":"agent"}',
    },
    "tool_call_result": {
        "tool_call_id": "tool-call-1",
        "status": "succeeded",
        "result_summary": "one result",
        "artifact_refs": [],
    },
    "approval_required": {
        "approval_id": "approval-1",
        "tool_name": "deploy",
        "parameter_digest": "sha256:approval",
        "expires_at": "2026-08-09T01:00:00Z",
    },
    "approval_resolved": {
        "approval_id": "approval-1",
        "decision": "APPROVED",
        "decided_by": "user-1",
    },
    "task_progress": {
        "task_id": "task-1",
        "current": 1,
        "total": 2,
        "message": "halfway",
    },
    "artifact_created": {
        "artifact_id": "artifact-1",
        "name": "report.txt",
        "content_type": "text/plain",
        "size": 12,
    },
    "warning": {"code": "PARTIAL", "message": "partial result"},
    "run_succeeded": {
        "result_message_id": "message-1",
        "usage": {
            "input_tokens": 3,
            "output_tokens": 5,
            "reasoning_tokens": 0,
            "estimated": False,
        },
        "warnings": [],
        "result_quality": "NORMAL",
    },
    "run_failed": {
        "error_code": "MODEL_UNAVAILABLE",
        "message": "model unavailable",
        "retryable": True,
    },
    "run_cancelled": {"reason": "user request", "cancelled_by": "user-1"},
    "run_timeout": {"timeout_seconds": 30, "stage": "runtime"},
}

EXPECTED_OUTPUTS: dict[str, tuple[tuple[str, str | None], ...]] = {
    "run_created": (("CUSTOM", "run_created"),),
    "run_queued": (("CUSTOM", "run_queued"),),
    "run_started": (("RUN_STARTED", None),),
    "text_message_start": (("TEXT_MESSAGE_START", None),),
    "text_delta": (("TEXT_MESSAGE_CONTENT", None),),
    "text_message_end": (("TEXT_MESSAGE_END", None),),
    "thinking_delta": (("CUSTOM", "thinking_delta"),),
    "plan_updated": (("CUSTOM", "plan_update"),),
    "tool_call_start": (("TOOL_CALL_START", None),),
    "tool_call_args": (("TOOL_CALL_ARGS", None),),
    "tool_call_result": (("TOOL_CALL_RESULT", None),),
    "approval_required": (("CUSTOM", "approval_required"),),
    "approval_resolved": (("CUSTOM", "approval_resolved"),),
    "task_progress": (("CUSTOM", "task_progress"),),
    "artifact_created": (("CUSTOM", "artifact_created"),),
    "warning": (("CUSTOM", "warning"),),
    "run_succeeded": (("RUN_FINISHED", None),),
    "run_failed": (("RUN_ERROR", None),),
    "run_cancelled": (("CUSTOM", "run_cancelled"), ("RUN_FINISHED", None)),
    "run_timeout": (("RUN_ERROR", None),),
}


def json_object(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return cast(dict[str, JsonValue], value)


def run_event(
    event_type: str,
    *,
    sequence_no: int = 1,
    payload: dict[str, JsonValue] | None = None,
) -> RunEvent:
    return RUN_EVENT_ADAPTER.validate_python(
        {
            "schema_version": "1.0",
            "event_id": f"event-{sequence_no}",
            "source_event_id": f"source-{sequence_no}",
            "tenant_id": "tenant-1",
            "run_id": "run-1",
            "session_id": "session-1",
            "sequence_no": sequence_no,
            "event_type": event_type,
            "occurred_at": NOW,
            "recorded_at": NOW,
            "trace_id": "trace-1",
            "execution_attempt": 1,
            "payload_version": "1.0",
            "payload": PAYLOADS[event_type] if payload is None else payload,
        }
    )


@pytest.mark.parametrize("event_type", list(PAYLOADS))
def test_adapter_maps_every_frozen_run_event_without_mutable_state(
    event_type: str,
) -> None:
    source = run_event(event_type)
    adapter = RunEventAgUiAdapter()

    first = adapter.map_event(source)
    second = adapter.map_event(source)

    assert first == second
    assert first.source_sequence_no == source.sequence_no
    actual = tuple(
        (
            serialized["type"],
            serialized.get("name") if serialized["type"] == "CUSTOM" else None,
        )
        for serialized in (serialize_ag_ui_event(item) for item in first.events)
    )
    assert actual == EXPECTED_OUTPUTS[event_type]
    for item in first.events:
        serialized = serialize_ag_ui_event(item)
        assert json_object(serialized["rawEvent"])["sequenceNo"] == source.sequence_no
        assert AG_UI_EVENT_ADAPTER.validate_python(serialized)


def test_adapter_preserves_redacted_thinking_and_uses_custom_event() -> None:
    mapped = RunEventAgUiAdapter().map_event(run_event("thinking_delta"))

    serialized = serialize_ag_ui_event(mapped.events[0])

    assert serialized["type"] == "CUSTOM"
    assert serialized["name"] == "thinking_delta"
    source = json_object(serialized["value"])
    payload = json_object(source["payload"])
    assert payload["delta"] == REDACTED_THINKING_DELTA


def test_tool_text_role_falls_back_without_impersonating_assistant() -> None:
    tool_start = run_event(
        "text_message_start",
        payload={"message_id": "message-1", "role": "tool"},
    )

    mapped = RunEventAgUiAdapter().map_event(tool_start)

    serialized = serialize_ag_ui_event(mapped.events[0])
    assert serialized["type"] == "CUSTOM"
    assert serialized["name"] == "text_message_start"
    source = json_object(serialized["value"])
    payload = json_object(source["payload"])
    assert payload["role"] == "tool"


def test_terminal_mapping_preserves_cancel_and_timeout_semantics() -> None:
    cancelled = RunEventAgUiAdapter().map_event(run_event("run_cancelled"))
    timeout = RunEventAgUiAdapter().map_event(run_event("run_timeout"))

    cancelled_events = [serialize_ag_ui_event(item) for item in cancelled.events]
    timeout_event = serialize_ag_ui_event(timeout.events[0])

    assert [item["type"] for item in cancelled_events] == [
        "CUSTOM",
        "RUN_FINISHED",
    ]
    assert cancelled_events[0]["name"] == "run_cancelled"
    assert timeout_event["type"] == "RUN_ERROR"
    assert timeout_event["code"] == "RUN_TIMEOUT"


def test_mapping_rejects_naive_source_timestamp() -> None:
    invalid = run_event("run_started").model_copy(
        update={"recorded_at": NOW.replace(tzinfo=None)}
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        RunEventAgUiAdapter().map_event(invalid)


def test_representative_mapping_matches_golden_snapshot() -> None:
    snapshot = {
        event_type: [
            serialize_ag_ui_event(item)
            for item in RunEventAgUiAdapter().map_event(run_event(event_type)).events
        ]
        for event_type in (
            "run_started",
            "text_delta",
            "plan_updated",
            "tool_call_result",
            "run_succeeded",
            "run_cancelled",
            "run_timeout",
        )
    }

    assert snapshot == json.loads(GOLDEN.read_text())

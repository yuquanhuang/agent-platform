"""Translate AgentScope events into frozen platform event candidates."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from agentscope.event import (
    AgentEvent,
    DataBlockDeltaEvent,
    DataBlockEndEvent,
    DataBlockStartEvent,
    ExceedMaxItersEvent,
    ExternalExecutionResultEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RequireExternalExecutionEvent,
    RequireUserConfirmEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ThinkingBlockDeltaEvent,
    ThinkingBlockEndEvent,
    ThinkingBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
    UserConfirmResultEvent,
    UserInterruptEvent,
)

from packages.contracts.generated.run_event import (
    RUNTIME_EVENT_CANDIDATE_ADAPTER,
    RuntimeEventCandidate,
)

_IGNORED_EVENT_TYPES = (
    ModelCallStartEvent,
    ModelCallEndEvent,
    TextBlockStartEvent,
    TextBlockEndEvent,
    ThinkingBlockStartEvent,
    ThinkingBlockEndEvent,
    ToolCallEndEvent,
)


class AgentScopeBoundaryError(RuntimeError):
    """Raised when an AgentScope event lacks required platform facts."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AgentScopeEventTranslator:
    """Stateful, idempotent translator scoped to one runtime execution."""

    def __init__(self) -> None:
        self._tool_results: dict[str, list[str]] = {}
        self._event_fingerprints: dict[str, dict[str, Any]] = {}
        self._translated: dict[str, RuntimeEventCandidate | None] = {}

    def translate(self, event: AgentEvent) -> RuntimeEventCandidate | None:
        """Translate one event, ignoring metadata and failing closed at bridges."""

        fingerprint = event.model_dump(mode="json", exclude={"metadata"})
        previous = self._event_fingerprints.get(event.id)
        if previous is not None:
            if previous != fingerprint:
                raise AgentScopeBoundaryError(
                    "AGENTSCOPE_EVENT_ID_COLLISION",
                    "AgentScope reused an event id for different event content.",
                )
            return self._translated[event.id]

        candidate = self._translate_new(event)
        self._event_fingerprints[event.id] = fingerprint
        self._translated[event.id] = candidate
        return candidate

    def _translate_new(self, event: AgentEvent) -> RuntimeEventCandidate | None:
        common = {
            "source_event_id": f"agentscope:{event.id}",
            "occurred_at": _parse_occurred_at(event.created_at),
            "payload_version": "1.0",
        }
        if isinstance(event, ReplyStartEvent):
            if event.role != "assistant":
                raise AgentScopeBoundaryError(
                    "AGENTSCOPE_UNSUPPORTED_REPLY_ROLE",
                    "Only assistant replies can become platform text messages.",
                )
            return _validate(
                common,
                "text_message_start",
                {"message_id": event.reply_id, "role": "assistant"},
            )
        if isinstance(event, TextBlockDeltaEvent):
            if not event.delta:
                return None
            return _validate(
                common,
                "text_delta",
                {"message_id": event.reply_id, "delta": event.delta},
            )
        if isinstance(event, ThinkingBlockDeltaEvent):
            if not event.delta:
                return None
            return _validate(
                common,
                "thinking_delta",
                {
                    "message_id": event.reply_id,
                    "delta": event.delta,
                    "visibility": "debug_only",
                },
            )
        if isinstance(event, ToolCallStartEvent):
            return _validate(
                common,
                "tool_call_start",
                {
                    "tool_call_id": event.tool_call_id,
                    "tool_name": event.tool_call_name,
                    "arguments_summary": "",
                },
            )
        if isinstance(event, ToolCallDeltaEvent):
            return _validate(
                common,
                "tool_call_args",
                {
                    "tool_call_id": event.tool_call_id,
                    "arguments_patch": event.delta,
                },
            )
        if isinstance(event, ToolResultStartEvent):
            self._tool_results[event.tool_call_id] = []
            return None
        if isinstance(event, ToolResultTextDeltaEvent):
            parts = self._tool_results.get(event.tool_call_id)
            if parts is None:
                raise AgentScopeBoundaryError(
                    "AGENTSCOPE_TOOL_RESULT_SEQUENCE_INVALID",
                    "A tool result delta arrived before its start event.",
                )
            parts.append(event.delta)
            return None
        if isinstance(event, ToolResultEndEvent):
            parts = self._tool_results.pop(event.tool_call_id, None)
            if parts is None:
                raise AgentScopeBoundaryError(
                    "AGENTSCOPE_TOOL_RESULT_SEQUENCE_INVALID",
                    "A tool result ended before its start event.",
                )
            status = {
                "success": "succeeded",
                "error": "failed",
                "interrupted": "cancelled",
                "denied": "denied",
            }.get(str(event.state))
            if status is None:
                raise AgentScopeBoundaryError(
                    "AGENTSCOPE_TOOL_RESULT_STATE_INVALID",
                    "AgentScope emitted a non-terminal tool result state.",
                )
            return _validate(
                common,
                "tool_call_result",
                {
                    "tool_call_id": event.tool_call_id,
                    "status": status,
                    "result_summary": "".join(parts)[:8000],
                    "artifact_refs": [],
                    "error_code": (
                        "AGENTSCOPE_TOOL_ERROR" if status == "failed" else None
                    ),
                },
            )
        if isinstance(event, ReplyEndEvent):
            finish_reason = {
                "completed": "stop",
                "interrupted": "cancelled",
                "exceed_max_iters": "length",
                "error": "error",
            }.get(str(event.finished_reason), "unknown")
            return _validate(
                common,
                "text_message_end",
                {"message_id": event.reply_id, "finish_reason": finish_reason},
            )
        if isinstance(event, ExceedMaxItersEvent):
            return _validate(
                common,
                "warning",
                {
                    "code": "AGENTSCOPE_MAX_ITERS",
                    "message": "The runtime reached its configured iteration limit.",
                    "details": None,
                },
            )
        if isinstance(
            event, (DataBlockStartEvent, DataBlockDeltaEvent, DataBlockEndEvent)
        ):
            raise AgentScopeBoundaryError(
                "PLATFORM_ARTIFACT_BRIDGE_REQUIRED",
                "Binary runtime output must be committed by the Artifact Service.",
            )
        if isinstance(event, ToolResultDataDeltaEvent):
            raise AgentScopeBoundaryError(
                "PLATFORM_ARTIFACT_BRIDGE_REQUIRED",
                "Binary tool output must be committed by the Artifact Service.",
            )
        if isinstance(event, RequireUserConfirmEvent):
            raise AgentScopeBoundaryError(
                "PLATFORM_APPROVAL_BRIDGE_REQUIRED",
                "Tool approval requires a platform approval fact.",
            )
        if isinstance(event, RequireExternalExecutionEvent):
            raise AgentScopeBoundaryError(
                "PLATFORM_EXTERNAL_EXECUTION_BRIDGE_REQUIRED",
                "External tool execution must be parked by the platform workflow.",
            )
        if isinstance(
            event,
            (UserConfirmResultEvent, ExternalExecutionResultEvent, UserInterruptEvent),
        ):
            raise AgentScopeBoundaryError(
                "AGENTSCOPE_CONTROL_EVENT_DIRECTION_INVALID",
                "Runtime control events cannot be published as platform output events.",
            )
        if isinstance(event, _IGNORED_EVENT_TYPES):
            return None
        raise AgentScopeBoundaryError(
            "AGENTSCOPE_EVENT_UNSUPPORTED",
            "The AgentScope event is not approved by the platform adapter.",
        )


def _parse_occurred_at(value: str) -> datetime:
    try:
        occurred_at = datetime.fromisoformat(value)
    except ValueError as error:
        raise AgentScopeBoundaryError(
            "AGENTSCOPE_EVENT_TIME_INVALID",
            "AgentScope emitted an invalid event timestamp.",
        ) from error
    if occurred_at.tzinfo is None:
        return occurred_at.replace(tzinfo=UTC)
    return occurred_at.astimezone(UTC)


def _validate(
    common: Mapping[str, object], event_type: str, payload: dict[str, object]
) -> RuntimeEventCandidate:
    return RUNTIME_EVENT_CANDIDATE_ADAPTER.validate_python(
        {**common, "event_type": event_type, "payload": payload}
    )

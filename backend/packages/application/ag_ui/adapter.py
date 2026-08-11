"""Stateless RunEvent-to-AG-UI v0.1.19 outbound mapping."""

from dataclasses import dataclass
from datetime import datetime
from typing import assert_never, cast

import ag_ui.core as ag_ui
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter
from pydantic.alias_generators import to_camel

from packages.contracts.generated.run_event import (
    ApprovalRequiredEvent,
    ApprovalResolvedEvent,
    ArtifactCreatedEvent,
    PlanUpdatedEvent,
    RunCancelledEvent,
    RunCreatedEvent,
    RunEvent,
    RunFailedEvent,
    RunQueuedEvent,
    RunStartedEvent,
    RunSucceededEvent,
    RunTimeoutEvent,
    TaskProgressEvent,
    TextDeltaEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ThinkingDeltaEvent,
    ToolCallArgsEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
    WarningEvent,
)

AG_UI_EVENT_ADAPTER: TypeAdapter[ag_ui.BaseEvent] = cast(
    TypeAdapter[ag_ui.BaseEvent], TypeAdapter(ag_ui.Event)
)


class PlatformAgUiSource(BaseModel):
    """Authorized source projection preserving frozen RunEvent payload semantics."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    event_id: str
    run_id: str
    sequence_no: int
    event_type: str
    payload: JsonValue


@dataclass(frozen=True, slots=True)
class AgUiEventBatch:
    """All AG-UI outputs produced atomically from one RunEvent sequence."""

    source_sequence_no: int
    events: tuple[ag_ui.BaseEvent, ...]

    def __post_init__(self) -> None:
        if self.source_sequence_no < 1:
            raise ValueError("AG-UI source_sequence_no must be positive")
        if not self.events:
            raise ValueError("AG-UI event batch must not be empty")


class RunEventAgUiAdapter:
    """Map already authorized/redacted facts without mutable lifecycle state."""

    def map_event(self, event: RunEvent) -> AgUiEventBatch:
        source = _source(event)
        timestamp = _timestamp_ms(event.recorded_at)
        output: tuple[ag_ui.BaseEvent, ...]

        match event:
            case RunCreatedEvent():
                output = (_custom("run_created", source, timestamp),)
            case RunQueuedEvent():
                output = (_custom("run_queued", source, timestamp),)
            case RunStartedEvent():
                output = (
                    ag_ui.RunStartedEvent(
                        thread_id=event.session_id,
                        run_id=event.run_id,
                        timestamp=timestamp,
                        raw_event=source,
                    ),
                )
            case TextMessageStartEvent():
                if event.payload.role == "assistant":
                    output = (
                        ag_ui.TextMessageStartEvent(
                            message_id=event.payload.message_id,
                            role="assistant",
                            timestamp=timestamp,
                            raw_event=source,
                        ),
                    )
                else:
                    # AG-UI text messages cannot use role=tool. Preserve the frozen
                    # RunEvent instead of silently changing its role.
                    output = (_custom("text_message_start", source, timestamp),)
            case TextDeltaEvent():
                output = (
                    ag_ui.TextMessageContentEvent(
                        message_id=event.payload.message_id,
                        delta=event.payload.delta,
                        timestamp=timestamp,
                        raw_event=source,
                    ),
                )
            case TextMessageEndEvent():
                output = (
                    ag_ui.TextMessageEndEvent(
                        message_id=event.payload.message_id,
                        timestamp=timestamp,
                        raw_event=source,
                    ),
                )
            case ThinkingDeltaEvent():
                output = (_custom("thinking_delta", source, timestamp),)
            case PlanUpdatedEvent():
                output = (_custom("plan_update", source, timestamp),)
            case ToolCallStartEvent():
                output = (
                    ag_ui.ToolCallStartEvent(
                        tool_call_id=event.payload.tool_call_id,
                        tool_call_name=event.payload.tool_name,
                        timestamp=timestamp,
                        raw_event=source,
                    ),
                )
            case ToolCallArgsEvent():
                output = (
                    ag_ui.ToolCallArgsEvent(
                        tool_call_id=event.payload.tool_call_id,
                        delta=event.payload.arguments_patch,
                        timestamp=timestamp,
                        raw_event=source,
                    ),
                )
            case ToolCallResultEvent():
                output = (
                    ag_ui.ToolCallResultEvent(
                        message_id=event.event_id,
                        tool_call_id=event.payload.tool_call_id,
                        content=event.payload.result_summary,
                        role="tool",
                        timestamp=timestamp,
                        raw_event=source,
                    ),
                )
            case ApprovalRequiredEvent():
                output = (_custom("approval_required", source, timestamp),)
            case ApprovalResolvedEvent():
                output = (_custom("approval_resolved", source, timestamp),)
            case TaskProgressEvent():
                output = (_custom("task_progress", source, timestamp),)
            case ArtifactCreatedEvent():
                output = (_custom("artifact_created", source, timestamp),)
            case WarningEvent():
                output = (_custom("warning", source, timestamp),)
            case RunSucceededEvent():
                output = (
                    ag_ui.RunFinishedEvent(
                        thread_id=event.session_id,
                        run_id=event.run_id,
                        result=source["payload"],
                        outcome=ag_ui.RunFinishedSuccessOutcome(),
                        timestamp=timestamp,
                        raw_event=source,
                    ),
                )
            case RunFailedEvent():
                output = (
                    ag_ui.RunErrorEvent(
                        message=event.payload.message,
                        code=event.payload.error_code,
                        timestamp=timestamp,
                        raw_event=source,
                    ),
                )
            case RunCancelledEvent():
                output = (
                    _custom("run_cancelled", source, timestamp),
                    ag_ui.RunFinishedEvent(
                        thread_id=event.session_id,
                        run_id=event.run_id,
                        result=source["payload"],
                        timestamp=timestamp,
                        raw_event=source,
                    ),
                )
            case RunTimeoutEvent():
                output = (
                    ag_ui.RunErrorEvent(
                        message=(
                            f"Run timed out during {event.payload.stage} after "
                            f"{event.payload.timeout_seconds} seconds."
                        ),
                        code="RUN_TIMEOUT",
                        timestamp=timestamp,
                        raw_event=source,
                    ),
                )
            case _ as unreachable:
                assert_never(unreachable)

        for mapped_event in output:
            AG_UI_EVENT_ADAPTER.validate_python(
                mapped_event.model_dump(mode="python", by_alias=True)
            )
        return AgUiEventBatch(
            source_sequence_no=event.sequence_no,
            events=output,
        )


def serialize_ag_ui_event(event: ag_ui.BaseEvent) -> dict[str, JsonValue]:
    """Serialize one validated official AG-UI event using camelCase fields."""

    validated = AG_UI_EVENT_ADAPTER.validate_python(
        event.model_dump(mode="python", by_alias=True)
    )
    return cast(
        dict[str, JsonValue],
        validated.model_dump(mode="json", by_alias=True, exclude_none=True),
    )


def _custom(
    name: str,
    source: dict[str, JsonValue],
    timestamp: int,
) -> ag_ui.CustomEvent:
    return ag_ui.CustomEvent(
        name=name,
        value=source,
        timestamp=timestamp,
        raw_event=source,
    )


def _source(event: RunEvent) -> dict[str, JsonValue]:
    payload = cast(JsonValue, event.payload.model_dump(mode="json", by_alias=True))
    return cast(
        dict[str, JsonValue],
        PlatformAgUiSource(
            event_id=event.event_id,
            run_id=event.run_id,
            sequence_no=event.sequence_no,
            event_type=event.event_type,
            payload=payload,
        ).model_dump(mode="json", by_alias=True),
    )


def _timestamp_ms(recorded_at: datetime) -> int:
    if recorded_at.tzinfo is None:
        raise ValueError("RunEvent recorded_at must be timezone-aware")
    return int(recorded_at.timestamp() * 1000)

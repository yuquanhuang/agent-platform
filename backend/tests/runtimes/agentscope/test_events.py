from datetime import UTC

import pytest
from agentscope.event import (
    DataBlockStartEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RequireExternalExecutionEvent,
    RequireUserConfirmEvent,
    TextBlockDeltaEvent,
    ThinkingBlockDeltaEvent,
    ToolCallDeltaEvent,
    ToolCallStartEvent,
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from agentscope.message import ToolCallBlock, ToolResultState
from agentscope.types import ReplyFinishedReason

from packages.runtimes.agentscope.events import (
    AgentScopeBoundaryError,
    AgentScopeEventTranslator,
)


def test_text_thinking_and_tool_events_map_to_frozen_candidates() -> None:
    translator = AgentScopeEventTranslator()

    started = translator.translate(
        ReplyStartEvent(
            id="evt-start",
            created_at="2026-08-07T10:00:00",
            session_id="runtime-session",
            reply_id="message-1",
            name="assistant",
            metadata={"authorization": "Bearer secret"},
        )
    )
    text = translator.translate(
        TextBlockDeltaEvent(
            id="evt-text",
            created_at="2026-08-07T10:00:01+08:00",
            reply_id="message-1",
            block_id="text-1",
            delta="hello",
        )
    )
    thinking = translator.translate(
        ThinkingBlockDeltaEvent(
            id="evt-thinking",
            created_at="2026-08-07T10:00:02Z",
            reply_id="message-1",
            block_id="thinking-1",
            delta="internal",
        )
    )
    tool_start = translator.translate(
        ToolCallStartEvent(
            id="evt-tool-start",
            created_at="2026-08-07T10:00:03Z",
            reply_id="message-1",
            tool_call_id="tool-1",
            tool_call_name="search",
        )
    )
    tool_args = translator.translate(
        ToolCallDeltaEvent(
            id="evt-tool-args",
            created_at="2026-08-07T10:00:04Z",
            reply_id="message-1",
            tool_call_id="tool-1",
            delta='{"query":"agent"}',
        )
    )

    assert started is not None
    assert started.event_type == "text_message_start"
    assert started.source_event_id == "agentscope:evt-start"
    assert started.occurred_at.tzinfo is UTC
    assert "secret" not in started.model_dump_json()
    assert text is not None and text.event_type == "text_delta"
    assert text.occurred_at.hour == 2
    assert thinking is not None and thinking.event_type == "thinking_delta"
    assert thinking.payload.visibility == "debug_only"
    assert tool_start is not None and tool_start.event_type == "tool_call_start"
    assert tool_args is not None and tool_args.event_type == "tool_call_args"


def test_tool_result_is_accumulated_limited_and_metadata_is_dropped() -> None:
    translator = AgentScopeEventTranslator()
    translator.translate(
        ToolResultStartEvent(
            id="result-start",
            reply_id="message-1",
            tool_call_id="tool-1",
            tool_call_name="search",
        )
    )
    delta = ToolResultTextDeltaEvent(
        id="result-delta",
        reply_id="message-1",
        tool_call_id="tool-1",
        delta="x" * 9000,
        metadata={"api_key": "must-not-leak"},
    )
    assert translator.translate(delta) is None
    assert translator.translate(delta) is None

    result = translator.translate(
        ToolResultEndEvent(
            id="result-end",
            created_at="2026-08-07T10:00:00Z",
            reply_id="message-1",
            tool_call_id="tool-1",
            state=ToolResultState.SUCCESS,
            metadata={"provider_response": "must-not-leak"},
        )
    )

    assert result is not None and result.event_type == "tool_call_result"
    assert len(result.payload.result_summary) == 8000
    assert result.payload.status == "succeeded"
    assert "must-not-leak" not in result.model_dump_json()
    assert (
        translator.translate(
            ToolResultEndEvent(
                id="result-end",
                created_at="2026-08-07T10:00:00Z",
                reply_id="message-1",
                tool_call_id="tool-1",
                state=ToolResultState.SUCCESS,
                metadata={"different_metadata_is_ignored": True},
            )
        )
        == result
    )


def test_reply_end_maps_runtime_reason_without_publishing_run_terminal() -> None:
    candidate = AgentScopeEventTranslator().translate(
        ReplyEndEvent(
            id="reply-end",
            session_id="runtime-session",
            reply_id="message-1",
            finished_reason=ReplyFinishedReason.INTERRUPTED,
        )
    )

    assert candidate is not None
    assert candidate.event_type == "text_message_end"
    assert candidate.payload.finish_reason == "cancelled"


@pytest.mark.parametrize(
    ("event", "expected_code"),
    [
        (
            RequireUserConfirmEvent(
                reply_id="message-1",
                tool_calls=[ToolCallBlock(id="tool-1", name="delete", input="{}")],
            ),
            "PLATFORM_APPROVAL_BRIDGE_REQUIRED",
        ),
        (
            RequireExternalExecutionEvent(
                reply_id="message-1",
                tool_calls=[ToolCallBlock(id="tool-1", name="search", input="{}")],
            ),
            "PLATFORM_EXTERNAL_EXECUTION_BRIDGE_REQUIRED",
        ),
        (
            ToolResultDataDeltaEvent(
                reply_id="message-1",
                tool_call_id="tool-1",
                media_type="image/png",
                data="aGVsbG8=",
            ),
            "PLATFORM_ARTIFACT_BRIDGE_REQUIRED",
        ),
        (
            DataBlockStartEvent(
                reply_id="message-1",
                block_id="data-1",
                media_type="image/png",
            ),
            "PLATFORM_ARTIFACT_BRIDGE_REQUIRED",
        ),
    ],
)
def test_missing_platform_facts_fail_closed(event: object, expected_code: str) -> None:
    with pytest.raises(AgentScopeBoundaryError) as raised:
        AgentScopeEventTranslator().translate(event)  # type: ignore[arg-type]

    assert raised.value.code == expected_code


def test_reused_event_id_with_different_content_fails_closed() -> None:
    translator = AgentScopeEventTranslator()
    translator.translate(
        TextBlockDeltaEvent(
            id="duplicate",
            reply_id="message-1",
            block_id="text-1",
            delta="first",
        )
    )

    with pytest.raises(AgentScopeBoundaryError) as raised:
        translator.translate(
            TextBlockDeltaEvent(
                id="duplicate",
                reply_id="message-1",
                block_id="text-1",
                delta="changed",
            )
        )

    assert raised.value.code == "AGENTSCOPE_EVENT_ID_COLLISION"

"""Fail-fast compatibility probe for the locked AgentScope runtime."""

from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import version
from inspect import signature
from sys import version_info
from typing import cast

from agentscope.agent import Agent
from agentscope.event import (
    EventBase,
    ExternalExecutionResultEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RequireExternalExecutionEvent,
    RequireUserConfirmEvent,
    TextBlockDeltaEvent,
    ThinkingBlockDeltaEvent,
    ToolCallDeltaEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultTextDeltaEvent,
    UserInterruptEvent,
)
from agentscope.model import ChatModelBase

AGENTSCOPE_LOCKED_VERSION = "2.0.5"
AGENTSCOPE_WHEEL_SHA256 = (
    "ae440075c8d72b21a0e6b54458b9c6b9d1d9594f1e3f885611df1ed93950d444"
)


@dataclass(frozen=True, slots=True)
class AgentScopeCompatibilityReport:
    """Verified API surface used by the isolated runtime adapter."""

    version: str
    python_version: str
    reply_stream_parameters: tuple[str, ...]
    model_call_parameters: tuple[str, ...]
    event_types: tuple[str, ...]


def _require_parameters(
    target: Callable[..., object], expected: tuple[str, ...]
) -> tuple[str, ...]:
    parameters = tuple(signature(target).parameters)
    missing = [name for name in expected if name not in parameters]
    if missing:
        raise RuntimeError(
            "The locked AgentScope API is incompatible with the platform adapter."
        )
    return parameters


def _require_event_fields(event_type: type[EventBase], fields: tuple[str, ...]) -> None:
    missing = [name for name in fields if name not in event_type.model_fields]
    if missing:
        raise RuntimeError(
            "The locked AgentScope event schema is incompatible with the platform "
            "adapter."
        )


def probe_agentscope_compatibility() -> AgentScopeCompatibilityReport:
    """Verify the exact package and public APIs without network or credentials."""

    resolved_version = version("agentscope")
    if resolved_version != AGENTSCOPE_LOCKED_VERSION:
        raise RuntimeError(
            "The installed AgentScope patch does not match the runtime baseline."
        )
    reply_parameters = _require_parameters(
        Agent.reply_stream,
        ("self", "inputs", "structured_schema", "yield_final_msg"),
    )
    model_parameters = _require_parameters(
        cast(Callable[..., object], ChatModelBase.__dict__["_call_api"]),
        ("self", "model_name", "messages", "tools", "tool_choice", "kwargs"),
    )
    required_events: tuple[tuple[type[EventBase], tuple[str, ...]], ...] = (
        (ReplyStartEvent, ("id", "created_at", "reply_id", "role")),
        (ReplyEndEvent, ("id", "created_at", "reply_id", "finished_reason")),
        (TextBlockDeltaEvent, ("id", "created_at", "reply_id", "delta")),
        (ThinkingBlockDeltaEvent, ("id", "created_at", "reply_id", "delta")),
        (ToolCallStartEvent, ("id", "tool_call_id", "tool_call_name")),
        (ToolCallDeltaEvent, ("id", "tool_call_id", "delta")),
        (ToolResultTextDeltaEvent, ("id", "tool_call_id", "delta")),
        (ToolResultEndEvent, ("id", "tool_call_id", "state")),
        (RequireUserConfirmEvent, ("id", "reply_id", "tool_calls")),
        (RequireExternalExecutionEvent, ("id", "reply_id", "tool_calls")),
        (ExternalExecutionResultEvent, ("id", "reply_id", "execution_results")),
        (UserInterruptEvent, ("id", "reply_id")),
    )
    for event_type, fields in required_events:
        _require_event_fields(event_type, fields)

    return AgentScopeCompatibilityReport(
        version=resolved_version,
        python_version=f"{version_info.major}.{version_info.minor}.{version_info.micro}",
        reply_stream_parameters=reply_parameters,
        model_call_parameters=model_parameters,
        event_types=tuple(event_type.__name__ for event_type, _ in required_events),
    )

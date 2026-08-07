"""Provider-specific OpenAI-compatible protocol profiles."""

from dataclasses import dataclass
from typing import Literal

ProviderName = Literal["openai", "qwen", "deepseek"]


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    provider: ProviderName
    request_id_headers: tuple[str, ...]
    allowed_parameters: frozenset[str]


COMMON_PARAMETERS = frozenset(
    {
        "frequency_penalty",
        "max_tokens",
        "presence_penalty",
        "seed",
        "temperature",
        "tool_choice",
        "top_p",
    }
)

OPENAI_PROFILE = ProviderProfile(
    provider="openai",
    request_id_headers=("x-request-id",),
    allowed_parameters=COMMON_PARAMETERS
    | {"max_completion_tokens", "parallel_tool_calls", "reasoning_effort"},
)

QWEN_PROFILE = ProviderProfile(
    provider="qwen",
    request_id_headers=("x-dashscope-request-id", "x-request-id"),
    allowed_parameters=COMMON_PARAMETERS
    | {"enable_thinking", "thinking_budget", "result_format"},
)

DEEPSEEK_PROFILE = ProviderProfile(
    provider="deepseek",
    request_id_headers=("x-request-id",),
    allowed_parameters=COMMON_PARAMETERS,
)

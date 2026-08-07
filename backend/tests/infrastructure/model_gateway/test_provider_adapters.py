"""Shared contract tests for OpenAI, Qwen and DeepSeek Provider Adapters."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from packages.application.model_gateway import ModelProviderAdapter
from packages.contracts.model_gateway import ImmutableReference, ModelGatewayRequest
from packages.domain.model_gateway import (
    AdapterStreamCompleted,
    AdapterStreamEvent,
    AdapterStreamStarted,
    AdapterTextDelta,
    AdapterToolCallDelta,
    AdapterUsageEvent,
    ModelInvocationInput,
    ModelRoute,
    ProviderConnectionTarget,
    ProviderError,
)
from packages.infrastructure.model_gateway import (
    DeepSeekProviderAdapter,
    OpenAIProviderAdapter,
    QwenProviderAdapter,
)

HASH = "sha256:" + "a" * 64


@dataclass(frozen=True, slots=True)
class AdapterCase:
    provider: str
    factory: Callable[[httpx.AsyncClient], ModelProviderAdapter]
    request_id_header: str
    usage: dict[str, object]
    expected_input: int
    expected_output: int
    expected_reasoning: int | None
    expected_cache_read: int | None


CASES = (
    AdapterCase(
        provider="openai",
        factory=OpenAIProviderAdapter,
        request_id_header="x-request-id",
        usage={
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "prompt_tokens_details": {"cached_tokens": 3},
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
        expected_input=11,
        expected_output=7,
        expected_reasoning=2,
        expected_cache_read=3,
    ),
    AdapterCase(
        provider="qwen",
        factory=QwenProviderAdapter,
        request_id_header="x-dashscope-request-id",
        usage={"input_tokens": 13, "output_tokens": 5},
        expected_input=13,
        expected_output=5,
        expected_reasoning=None,
        expected_cache_read=None,
    ),
    AdapterCase(
        provider="deepseek",
        factory=DeepSeekProviderAdapter,
        request_id_header="x-request-id",
        usage={
            "prompt_tokens": 17,
            "completion_tokens": 9,
            "prompt_cache_hit_tokens": 4,
            "completion_tokens_details": {"reasoning_tokens": 6},
        },
        expected_input=17,
        expected_output=9,
        expected_reasoning=6,
        expected_cache_read=4,
    ),
)


def gateway_request(
    *,
    stream: bool = False,
    parameters: dict[str, str | float | int | bool | None] | None = None,
) -> ModelGatewayRequest:
    return ModelGatewayRequest(
        schema_version="1.0",
        tenant_id="11111111-1111-4111-8111-111111111111",
        user_id="user-1",
        agent_id="agent-1",
        snapshot_id="snapshot-1",
        run_id="run-1",
        model_binding_id="binding-1",
        prompt_ref=ImmutableReference(uri="prompt://p1", hash=HASH),
        tools_ref=ImmutableReference(uri="tools://t1", hash=HASH),
        capability_requirements=["stream"] if stream else ["tools"],
        parameters=parameters or {},
        stream=stream,
        timeout_seconds=5,
        token_budget=None,
        cost_budget=None,
        idempotency_key="idempotency-123",
        authorization_token="authorization-token-1234",
    )


def invocation() -> ModelInvocationInput:
    return ModelInvocationInput(
        messages=({"role": "user", "content": "hello"},),
        tools=(
            {
                "type": "function",
                "function": {
                    "name": "lookup_weather",
                    "description": "Lookup weather",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ),
    )


def route(case: AdapterCase) -> ModelRoute:
    return ModelRoute(
        provider=case.provider,
        model="contract-model",
        base_url="https://provider.example/v1",
        secret_ref="secret://tenant/test/provider",
        capabilities=frozenset({"stream", "tools"}),
        timeout_seconds=5,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.provider)
async def test_generate_maps_text_request_id_and_provider_usage(
    case: AdapterCase,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer provider-secret"
        assert request.headers["Idempotency-Key"] == "idempotency-123"
        body = cast(dict[str, object], json.loads(request.content))
        assert body["model"] == "contract-model"
        assert body["messages"] == [{"role": "user", "content": "hello"}]
        tools = cast(list[dict[str, object]], body["tools"])
        function = cast(dict[str, object], tools[0]["function"])
        assert function["name"] == "lookup_weather"
        assert body["stream"] is False
        return httpx.Response(
            200,
            headers={case.request_id_header: f"{case.provider}-header-request"},
            json={
                "id": f"{case.provider}-body-request",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"content": "hello from provider"},
                    }
                ],
                "usage": case.usage,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await case.factory(client).generate(
            route(case),
            gateway_request(parameters={"temperature": 0.2}),
            invocation(),
            SecretStr("provider-secret"),
        )

    assert response.provider_request_id == f"{case.provider}-body-request"
    assert response.output == {"text": "hello from provider"}
    assert response.usage is not None
    assert response.usage.input_tokens == case.expected_input
    assert response.usage.output_tokens == case.expected_output
    assert response.usage.reasoning_tokens == case.expected_reasoning
    assert response.usage.cache_read_tokens == case.expected_cache_read


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.provider)
async def test_generate_maps_tool_calls(case: AdapterCase) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "tool-request",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup_weather",
                                        "arguments": '{"city":"Hangzhou"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await case.factory(client).generate(
            route(case), gateway_request(), invocation(), SecretStr("secret")
        )

    assert response.finish_reason == "tool_call"
    assert isinstance(response.output, dict)
    tool_calls = response.output["tool_calls"]
    assert isinstance(tool_calls, list)
    first_tool_call = tool_calls[0]
    assert isinstance(first_tool_call, dict)
    assert first_tool_call["id"] == "call-1"


@pytest.mark.asyncio
async def test_request_parameters_override_frozen_binding_defaults() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = cast(dict[str, object], json.loads(request.content))
        assert body["temperature"] == 0.8
        assert body["top_p"] == 0.9
        return httpx.Response(
            200,
            json={
                "id": "parameter-merge",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"content": "ok"},
                    }
                ],
            },
        )

    frozen_route = ModelRoute(
        provider="openai",
        model="contract-model",
        base_url="https://provider.example/v1",
        secret_ref="secret://tenant/test/provider",
        capabilities=frozenset(),
        timeout_seconds=5,
        default_parameters={"temperature": 0.2, "top_p": 0.9},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await OpenAIProviderAdapter(client).generate(
            frozen_route,
            gateway_request(parameters={"temperature": 0.8}),
            invocation(),
            SecretStr("provider-secret"),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.provider)
async def test_stream_maps_delta_tool_usage_and_terminal_output(
    case: AdapterCase,
) -> None:
    events: list[dict[str, object]] = [
        {
            "id": f"{case.provider}-stream",
            "choices": [{"index": 0, "delta": {"content": "hel"}}],
        },
        {
            "id": f"{case.provider}-stream",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-stream",
                                "function": {
                                    "name": "lookup_weather",
                                    "arguments": '{"city":',
                                },
                            }
                        ]
                    },
                }
            ],
        },
        {
            "id": f"{case.provider}-stream",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": '"Hangzhou"}'},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        },
        {"id": f"{case.provider}-stream", "choices": [], "usage": case.usage},
    ]
    stream_body = (
        "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        + "data: [DONE]\n\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        body = cast(dict[str, object], json.loads(request.content))
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        return httpx.Response(
            200,
            headers={case.request_id_header: f"{case.provider}-header"},
            text=stream_body,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        received = [
            event
            async for event in case.factory(client).stream(
                route(case),
                gateway_request(stream=True),
                invocation(),
                SecretStr("provider-secret"),
            )
        ]

    assert isinstance(received[0], AdapterStreamStarted)
    assert any(isinstance(event, AdapterTextDelta) for event in received)
    assert sum(isinstance(event, AdapterToolCallDelta) for event in received) == 2
    usage_event = next(
        event for event in received if isinstance(event, AdapterUsageEvent)
    )
    assert usage_event.usage.input_tokens == case.expected_input
    completed = received[-1]
    assert isinstance(completed, AdapterStreamCompleted)
    assert completed.finish_reason == "tool_call"
    assert isinstance(completed.output, dict)
    assert completed.output["text"] == "hel"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.provider)
@pytest.mark.parametrize(
    ("status_code", "expected_code", "retryable", "submission_state"),
    [
        (401, "AUTHENTICATION_FAILED", False, "not_submitted"),
        (429, "RATE_LIMITED", True, "not_submitted"),
        (503, "PROVIDER_UNAVAILABLE", True, "not_submitted"),
        (500, "PROVIDER_UNAVAILABLE", True, "unknown"),
    ],
)
async def test_http_errors_are_normalized_without_secret_or_raw_message(
    case: AdapterCase,
    status_code: int,
    expected_code: str,
    retryable: bool,
    submission_state: Literal["not_submitted", "submitted", "unknown"],
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={
                "error": {
                    "code": "provider-specific-code",
                    "message": "raw provider body with provider-secret",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as error:
            await case.factory(client).generate(
                route(case),
                gateway_request(),
                invocation(),
                SecretStr("provider-secret"),
            )

    assert error.value.code == expected_code
    assert error.value.retryable is retryable
    assert error.value.submission_state == submission_state
    assert error.value.provider_code == "provider-specific-code"
    assert "provider-secret" not in str(error.value)
    assert "raw provider body" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.provider)
async def test_connection_test_uses_models_endpoint(case: AdapterCase) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/models"
        assert request.headers["Authorization"] == "Bearer provider-secret"
        return httpx.Response(200, json={"data": []})

    target = ProviderConnectionTarget(
        provider_id=UUID("11111111-1111-4111-8111-111111111111"),
        provider=case.provider,
        base_url="https://provider.example/v1",
        secret_ref="secret://tenant/test/provider",
        timeout_seconds=5,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await case.factory(client).test_connection(target, SecretStr("provider-secret"))


@pytest.mark.asyncio
async def test_connect_failure_allows_fallback_but_read_timeout_does_not() -> None:
    async def connect_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("provider-secret must not leak", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(connect_failure)
    ) as client:
        with pytest.raises(ProviderError) as connection_error:
            await OpenAIProviderAdapter(client).generate(
                route(CASES[0]),
                gateway_request(),
                invocation(),
                SecretStr("provider-secret"),
            )
    assert connection_error.value.allows_fallback is True
    assert "provider-secret" not in str(connection_error.value)

    async def read_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(read_timeout)) as client:
        with pytest.raises(ProviderError) as timeout_error:
            await OpenAIProviderAdapter(client).generate(
                route(CASES[0]),
                gateway_request(),
                invocation(),
                SecretStr("provider-secret"),
            )
    assert timeout_error.value.submission_state == "unknown"
    assert timeout_error.value.allows_fallback is False


@pytest.mark.asyncio
async def test_invalid_base_url_parameter_and_stream_are_rejected() -> None:
    async def unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unexpected_request)
    ) as client:
        adapter = OpenAIProviderAdapter(client)
        with pytest.raises(ProviderError) as unsafe_url:
            await adapter.generate(
                ModelRoute(
                    provider="openai",
                    model="model",
                    base_url="https://user:secret@provider.example/v1",
                    secret_ref="secret://tenant/test/provider",
                    capabilities=frozenset(),
                    timeout_seconds=5,
                ),
                gateway_request(),
                invocation(),
                SecretStr("provider-secret"),
            )
        assert unsafe_url.value.code == "PROVIDER_CONFIGURATION_INVALID"

        with pytest.raises(ProviderError) as unsupported_parameter:
            await adapter.generate(
                route(CASES[0]),
                gateway_request(parameters={"provider_secret": "must-not-send"}),
                invocation(),
                SecretStr("provider-secret"),
            )
        assert unsupported_parameter.value.code == "INVALID_REQUEST"

    async def malformed_stream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='data: {"choices":"invalid"}\n\n')

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(malformed_stream)
    ) as client:
        with pytest.raises(ProviderError) as protocol_error:
            async for _ in OpenAIProviderAdapter(client).stream(
                route(CASES[0]),
                gateway_request(stream=True),
                invocation(),
                SecretStr("provider-secret"),
            ):
                pass
        assert protocol_error.value.code == "PROVIDER_PROTOCOL_ERROR"
        assert protocol_error.value.submission_state == "submitted"


class _BlockingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.blocked = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b'data: {"id":"cancel-1","choices":[]}\n\n'
        self.blocked.set()
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_stream_cancellation_is_not_swallowed() -> None:
    stream = _BlockingStream()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAIProviderAdapter(client)

        async def consume() -> list[AdapterStreamEvent]:
            return [
                event
                async for event in adapter.stream(
                    route(CASES[0]),
                    gateway_request(stream=True),
                    invocation(),
                    SecretStr("provider-secret"),
                )
            ]

        task = asyncio.create_task(consume())
        await asyncio.wait_for(stream.blocked.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

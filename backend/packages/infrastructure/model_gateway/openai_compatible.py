"""Safe OpenAI-compatible HTTP/SSE transport for supported model providers."""

import ipaddress
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Literal, cast
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, ValidationError

from packages.contracts.model_gateway import ModelGatewayRequest
from packages.domain.model_gateway import (
    AdapterResponse,
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
    ProviderUsage,
)
from packages.infrastructure.model_gateway.profiles import (
    DEEPSEEK_PROFILE,
    OPENAI_PROFILE,
    QWEN_PROFILE,
    ProviderProfile,
)

MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_ERROR_BYTES = 64 * 1024
MAX_SSE_LINE_BYTES = 1024 * 1024
CHAT_COMPLETIONS_PATH = "/chat/completions"
MODELS_PATH = "/models"


class _TokenDetails(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cached_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)


class _UsagePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    prompt_cache_hit_tokens: int | None = Field(default=None, ge=0)
    prompt_cache_miss_tokens: int | None = Field(default=None, ge=0)
    prompt_tokens_details: _TokenDetails | None = None
    completion_tokens_details: _TokenDetails | None = None


class _FunctionCall(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    arguments: str = ""


class _ToolCall(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    type: str = "function"
    function: _FunctionCall


class _Message(BaseModel):
    model_config = ConfigDict(extra="ignore")

    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[_ToolCall] = Field(default_factory=list[_ToolCall])


class _Choice(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index: int = 0
    finish_reason: str | None = None
    message: _Message


class _CompletionPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    choices: list[_Choice] = Field(min_length=1)
    usage: _UsagePayload | None = None


class _DeltaToolCall(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index: int = Field(ge=0)
    id: str | None = None
    function: _FunctionCall | None = None


class _Delta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[_DeltaToolCall] = Field(default_factory=list[_DeltaToolCall])


class _StreamChoice(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index: int = 0
    finish_reason: str | None = None
    delta: _Delta


class _StreamPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    choices: list[_StreamChoice] = Field(default_factory=list[_StreamChoice])
    usage: _UsagePayload | None = None


class _ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str | int | None = None


class _ErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    error: _ErrorDetail


@dataclass(slots=True)
class _ToolAccumulator:
    tool_call_id: str | None = None
    name: str | None = None
    arguments: str = ""


class OpenAICompatibleProviderAdapter:
    def __init__(self, client: httpx.AsyncClient, profile: ProviderProfile) -> None:
        self._client = client
        self._profile = profile

    async def generate(
        self,
        route: ModelRoute,
        request: ModelGatewayRequest,
        invocation: ModelInvocationInput,
        credential: SecretStr,
    ) -> AdapterResponse:
        self._validate_route(route)
        body = self._request_body(route, request, invocation, stream=False)
        try:
            response = await self._client.post(
                _provider_url(route.base_url, CHAT_COMPLETIONS_PATH),
                headers=_headers(credential, request.idempotency_key),
                json=body,
                timeout=httpx.Timeout(route.timeout_seconds),
                follow_redirects=False,
            )
        except httpx.HTTPError as error:
            raise _transport_error(error) from error
        await _raise_for_status(response)
        payload = _completion_payload(response)
        choice = payload.choices[0]
        finish_reason = _finish_reason(choice.finish_reason)
        output = _message_output(choice.message)
        return AdapterResponse(
            provider_request_id=payload.id or self._request_id(response.headers),
            finish_reason=finish_reason,
            output=output,
            usage=_usage(self._profile, payload.usage),
        )

    async def stream(
        self,
        route: ModelRoute,
        request: ModelGatewayRequest,
        invocation: ModelInvocationInput,
        credential: SecretStr,
    ) -> AsyncIterator[AdapterStreamEvent]:
        self._validate_route(route)
        body = self._request_body(route, request, invocation, stream=True)
        try:
            async with self._client.stream(
                "POST",
                _provider_url(route.base_url, CHAT_COMPLETIONS_PATH),
                headers=_headers(credential, request.idempotency_key),
                json=body,
                timeout=httpx.Timeout(route.timeout_seconds),
                follow_redirects=False,
            ) as response:
                await _raise_for_status(response)
                provider_request_id = self._request_id(response.headers)
                yield AdapterStreamStarted(provider_request_id=provider_request_id)
                text_parts: list[str] = []
                reasoning_parts: list[str] = []
                tool_calls: dict[int, _ToolAccumulator] = {}
                finish_reason: Literal[
                    "stop", "length", "tool_call", "cancelled", "unknown"
                ] = "unknown"
                async for line in response.aiter_lines():
                    if len(line.encode()) > MAX_SSE_LINE_BYTES:
                        raise _protocol_error("The provider stream line is too large.")
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data:
                        continue
                    if data == "[DONE]":
                        yield AdapterStreamCompleted(
                            finish_reason=finish_reason,
                            output=_stream_output(
                                text_parts, reasoning_parts, tool_calls
                            ),
                            provider_request_id=provider_request_id,
                        )
                        return
                    payload = _stream_payload(data)
                    provider_request_id = payload.id or provider_request_id
                    if payload.usage is not None:
                        yield AdapterUsageEvent(
                            usage=_usage(self._profile, payload.usage),
                            provider_request_id=provider_request_id,
                        )
                    choice = next(
                        (item for item in payload.choices if item.index == 0), None
                    )
                    if choice is None:
                        continue
                    if choice.delta.content:
                        text_parts.append(choice.delta.content)
                        yield AdapterTextDelta(
                            delta=choice.delta.content,
                            provider_request_id=provider_request_id,
                        )
                    if choice.delta.reasoning_content:
                        reasoning_parts.append(choice.delta.reasoning_content)
                    for tool_delta in choice.delta.tool_calls:
                        accumulator = tool_calls.setdefault(
                            tool_delta.index, _ToolAccumulator()
                        )
                        if tool_delta.id is not None:
                            accumulator.tool_call_id = tool_delta.id
                        function = tool_delta.function
                        if function is not None:
                            if function.name is not None:
                                accumulator.name = function.name
                            accumulator.arguments += function.arguments
                        if accumulator.tool_call_id is None:
                            raise _protocol_error(
                                "The provider omitted a tool call identifier."
                            )
                        yield AdapterToolCallDelta(
                            tool_call_id=accumulator.tool_call_id,
                            tool_name=function.name if function is not None else None,
                            arguments_patch=(
                                function.arguments if function is not None else ""
                            ),
                            provider_request_id=provider_request_id,
                        )
                    if choice.finish_reason is not None:
                        finish_reason = _finish_reason(choice.finish_reason)
                raise _protocol_error(
                    "The provider stream ended without a completion marker."
                )
        except httpx.HTTPError as error:
            raise _transport_error(error) from error

    async def test_connection(
        self, target: ProviderConnectionTarget, credential: SecretStr
    ) -> None:
        if target.provider != self._profile.provider:
            raise _configuration_error()
        try:
            response = await self._client.get(
                _provider_url(target.base_url, MODELS_PATH),
                headers=_headers(credential, "connection-test"),
                timeout=httpx.Timeout(target.timeout_seconds),
                follow_redirects=False,
            )
        except httpx.HTTPError as error:
            raise _transport_error(error) from error
        await _raise_for_status(response)

    def _validate_route(self, route: ModelRoute) -> None:
        if route.provider != self._profile.provider:
            raise _configuration_error()

    def _request_body(
        self,
        route: ModelRoute,
        request: ModelGatewayRequest,
        invocation: ModelInvocationInput,
        *,
        stream: bool,
    ) -> dict[str, object]:
        parameters = {**route.default_parameters, **request.parameters}
        if route.max_output_tokens is not None:
            requested_cap = parameters.get("max_completion_tokens")
            if requested_cap is None:
                requested_cap = parameters.get("max_tokens")
            if requested_cap is not None and (
                not isinstance(requested_cap, int)
                or isinstance(requested_cap, bool)
                or requested_cap < 1
                or requested_cap > route.max_output_tokens
            ):
                raise ProviderError(
                    code="INVALID_REQUEST",
                    message="The requested output limit exceeds the frozen model binding.",
                    retryable=False,
                    submission_state="not_submitted",
                )
            output_parameter = (
                "max_completion_tokens"
                if self._profile.provider == "openai"
                else "max_tokens"
            )
            parameters.pop("max_tokens", None)
            parameters.pop("max_completion_tokens", None)
            parameters[output_parameter] = requested_cap or route.max_output_tokens
        if route.max_reasoning_tokens is not None:
            if self._profile.provider != "qwen":
                raise ProviderError(
                    code="COST_BOUND_UNAVAILABLE",
                    message="The provider cannot enforce the frozen reasoning limit.",
                    retryable=False,
                    submission_state="not_submitted",
                )
            reasoning_parameter = "thinking_budget"
            requested_reasoning = parameters.get(reasoning_parameter)
            if requested_reasoning is not None and (
                not isinstance(requested_reasoning, int)
                or isinstance(requested_reasoning, bool)
                or requested_reasoning < 1
                or requested_reasoning > route.max_reasoning_tokens
            ):
                raise ProviderError(
                    code="INVALID_REQUEST",
                    message="The requested reasoning limit exceeds the frozen model binding.",
                    retryable=False,
                    submission_state="not_submitted",
                )
            parameters[reasoning_parameter] = (
                requested_reasoning or route.max_reasoning_tokens
            )
        unsupported = set(parameters) - self._profile.allowed_parameters
        if unsupported:
            raise ProviderError(
                code="INVALID_REQUEST",
                message="The model request contains unsupported provider parameters.",
                retryable=False,
                submission_state="not_submitted",
            )
        body: dict[str, object] = {
            "model": route.model,
            "messages": [dict(message) for message in invocation.messages],
            "stream": stream,
            **parameters,
        }
        if invocation.tools:
            body["tools"] = [dict(tool) for tool in invocation.tools]
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body

    def _request_id(self, headers: httpx.Headers) -> str | None:
        for header in self._profile.request_id_headers:
            value = headers.get(header)
            if value:
                return value[:512]
        return None


class OpenAIProviderAdapter(OpenAICompatibleProviderAdapter):
    def __init__(self, client: httpx.AsyncClient) -> None:
        super().__init__(client, OPENAI_PROFILE)


class QwenProviderAdapter(OpenAICompatibleProviderAdapter):
    def __init__(self, client: httpx.AsyncClient) -> None:
        super().__init__(client, QWEN_PROFILE)


class DeepSeekProviderAdapter(OpenAICompatibleProviderAdapter):
    def __init__(self, client: httpx.AsyncClient) -> None:
        super().__init__(client, DEEPSEEK_PROFILE)


def _headers(credential: SecretStr, idempotency_key: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {credential.get_secret_value()}",
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key,
        "User-Agent": "agent-platform-model-gateway/1",
    }


def _provider_url(base_url: str, suffix: str) -> str:
    parsed = urlsplit(base_url)
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.hostname is None
    ):
        raise _configuration_error()
    if not (
        parsed.scheme == "https"
        or (parsed.scheme == "http" and _is_loopback(parsed.hostname))
    ):
        raise _configuration_error()
    path = f"{parsed.path.rstrip('/')}{suffix}"
    return urlunsplit(SplitResult(parsed.scheme, parsed.netloc, path, "", ""))


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


async def _raise_for_status(response: httpx.Response) -> None:
    if 200 <= response.status_code < 300:
        return
    provider_code = await _safe_provider_code(response)
    status = response.status_code
    if status in {401, 403}:
        raise ProviderError(
            code="AUTHENTICATION_FAILED",
            message="The model provider rejected its credential.",
            retryable=False,
            submission_state="not_submitted",
            provider_code=provider_code,
        )
    if status == 429:
        raise ProviderError(
            code="RATE_LIMITED",
            message="The model provider rate limit was reached.",
            retryable=True,
            submission_state="not_submitted",
            provider_code=provider_code,
        )
    if status in {400, 404, 409, 422}:
        raise ProviderError(
            code="INVALID_REQUEST",
            message="The model provider rejected the request.",
            retryable=False,
            submission_state="not_submitted",
            provider_code=provider_code,
        )
    if status in {502, 503}:
        raise ProviderError(
            code="PROVIDER_UNAVAILABLE",
            message="The model provider is unavailable.",
            retryable=True,
            submission_state="not_submitted",
            provider_code=provider_code,
        )
    if status in {500, 504}:
        raise ProviderError(
            code="PROVIDER_UNAVAILABLE",
            message="The model provider failed with an unknown submission state.",
            retryable=True,
            submission_state="unknown",
            provider_code=provider_code,
        )
    if 300 <= status < 400:
        raise ProviderError(
            code="PROVIDER_REDIRECT_REJECTED",
            message="The model provider returned a redirect.",
            retryable=False,
            submission_state="not_submitted",
            provider_code=provider_code,
        )
    raise ProviderError(
        code="PROVIDER_ERROR",
        message="The model provider request failed.",
        retryable=False,
        submission_state="unknown",
        provider_code=provider_code,
    )


async def _safe_provider_code(response: httpx.Response) -> str | None:
    content = await response.aread()
    if len(content) > MAX_ERROR_BYTES:
        return None
    try:
        envelope = _ErrorEnvelope.model_validate_json(content)
    except (ValidationError, ValueError):
        return None
    return str(envelope.error.code)[:128] if envelope.error.code is not None else None


def _completion_payload(response: httpx.Response) -> _CompletionPayload:
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise _protocol_error("The provider response is too large.")
    try:
        return _CompletionPayload.model_validate_json(response.content)
    except ValidationError as error:
        provider_error = _provider_error_envelope(response.content)
        if provider_error is not None:
            raise provider_error from error
        raise _protocol_error("The provider returned an invalid response.") from error


def _stream_payload(data: str) -> _StreamPayload:
    try:
        return _StreamPayload.model_validate_json(data)
    except ValidationError as error:
        provider_error = _provider_error_envelope(data.encode())
        if provider_error is not None:
            raise provider_error from error
        raise _protocol_error(
            "The provider returned an invalid stream event."
        ) from error


def _provider_error_envelope(content: bytes) -> ProviderError | None:
    if len(content) > MAX_ERROR_BYTES:
        return None
    try:
        envelope = _ErrorEnvelope.model_validate_json(content)
    except (ValidationError, ValueError):
        return None
    provider_code = (
        str(envelope.error.code)[:128] if envelope.error.code is not None else None
    )
    return ProviderError(
        code="PROVIDER_ERROR",
        message="The model provider returned an error.",
        retryable=False,
        submission_state="unknown",
        provider_code=provider_code,
    )


def _message_output(message: _Message) -> JsonValue:
    output: dict[str, JsonValue] = {"text": message.content or ""}
    if message.reasoning_content:
        output["reasoning"] = message.reasoning_content
    if message.tool_calls:
        output["tool_calls"] = [
            cast(
                JsonValue,
                {
                    "id": tool_call.id,
                    "type": tool_call.type,
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                },
            )
            for tool_call in message.tool_calls
        ]
    return output


def _stream_output(
    text_parts: list[str],
    reasoning_parts: list[str],
    tool_calls: Mapping[int, _ToolAccumulator],
) -> JsonValue:
    output: dict[str, JsonValue] = {"text": "".join(text_parts)}
    if reasoning_parts:
        output["reasoning"] = "".join(reasoning_parts)
    if tool_calls:
        completed_tools: list[JsonValue] = []
        for _, tool_call in sorted(tool_calls.items()):
            if tool_call.tool_call_id is None:
                raise _protocol_error("The provider omitted a tool call identifier.")
            completed_tools.append(
                {
                    "id": tool_call.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": tool_call.name,
                        "arguments": tool_call.arguments,
                    },
                }
            )
        output["tool_calls"] = completed_tools
    return output


def _usage(profile: ProviderProfile, usage: _UsagePayload | None) -> ProviderUsage:
    if usage is None:
        return ProviderUsage()
    input_tokens = usage.prompt_tokens
    output_tokens = usage.completion_tokens
    if profile.provider == "qwen":
        input_tokens = input_tokens if input_tokens is not None else usage.input_tokens
        output_tokens = (
            output_tokens if output_tokens is not None else usage.output_tokens
        )
    cache_read_tokens = None
    if usage.prompt_tokens_details is not None:
        cache_read_tokens = usage.prompt_tokens_details.cached_tokens
    if profile.provider == "deepseek" and usage.prompt_cache_hit_tokens is not None:
        cache_read_tokens = usage.prompt_cache_hit_tokens
    reasoning_tokens = None
    if usage.completion_tokens_details is not None:
        reasoning_tokens = usage.completion_tokens_details.reasoning_tokens
    return ProviderUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=None,
        cost=None,
    )


def _finish_reason(
    value: str | None,
) -> Literal["stop", "length", "tool_call", "cancelled", "unknown"]:
    if value is None:
        return "unknown"
    if value in {"stop", "length"}:
        return cast(Literal["stop", "length"], value)
    if value in {"tool_call", "tool_calls", "function_call"}:
        return "tool_call"
    if value == "cancelled":
        return "cancelled"
    if value == "content_filter":
        raise ProviderError(
            code="CONTENT_FILTERED",
            message="The model provider filtered the response.",
            retryable=False,
            submission_state="submitted",
        )
    return "unknown"


def _transport_error(error: httpx.HTTPError) -> ProviderError:
    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return ProviderError(
            code="PROVIDER_UNAVAILABLE",
            message="The model provider could not be reached.",
            retryable=True,
            submission_state="not_submitted",
        )
    if isinstance(error, httpx.TimeoutException):
        return ProviderError(
            code="PROVIDER_TIMEOUT",
            message="The model provider timed out.",
            retryable=True,
            submission_state="unknown",
        )
    return ProviderError(
        code="PROVIDER_UNAVAILABLE",
        message="The model provider connection failed.",
        retryable=True,
        submission_state="unknown",
    )


def _configuration_error() -> ProviderError:
    return ProviderError(
        code="PROVIDER_CONFIGURATION_INVALID",
        message="The model provider configuration is invalid.",
        retryable=False,
        submission_state="not_submitted",
    )


def _protocol_error(message: str) -> ProviderError:
    return ProviderError(
        code="PROVIDER_PROTOCOL_ERROR",
        message=message,
        retryable=False,
        submission_state="submitted",
    )

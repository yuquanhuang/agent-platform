"""Cross-component tests for the Gateway and real provider adapters."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import httpx
import pytest
from pydantic import JsonValue, SecretStr

from packages.application.model_gateway import (
    ModelGatewayService,
    ModelProviderAdapter,
    ProviderAdapterRegistry,
)
from packages.contracts.model_gateway import (
    ImmutableReference,
    ModelGatewayRequest,
    ResponseCompletedEvent,
)
from packages.contracts.public import SubjectType, TenantContext
from packages.domain.model_gateway import (
    ModelBinding,
    ModelInvocationInput,
    ModelRoute,
    ModelUsageRecord,
    ProviderError,
)
from packages.infrastructure.model_gateway import (
    DeepSeekProviderAdapter,
    OpenAIProviderAdapter,
    QwenProviderAdapter,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
HASH = "sha256:" + "a" * 64


@dataclass(frozen=True, slots=True)
class ProviderCase:
    provider: str
    factory: Callable[[httpx.AsyncClient], ModelProviderAdapter]
    request_id_header: str


CASES = (
    ProviderCase("openai", OpenAIProviderAdapter, "x-request-id"),
    ProviderCase("qwen", QwenProviderAdapter, "x-dashscope-request-id"),
    ProviderCase("deepseek", DeepSeekProviderAdapter, "x-request-id"),
)


def tenant_context() -> TenantContext:
    return TenantContext(
        tenant_id=str(TENANT_ID),
        subject_type=SubjectType.USER,
        subject_id=str(ACTOR_ID),
        membership_version=1,
        auth_time=datetime(2026, 8, 7, tzinfo=UTC),
        request_id="req-provider-integration",
        trace_id="trace-provider-integration",
    )


def gateway_request(*, stream: bool = False) -> ModelGatewayRequest:
    return ModelGatewayRequest(
        schema_version="1.0",
        tenant_id=str(TENANT_ID),
        user_id="user-1",
        agent_id="agent-1",
        snapshot_id="snapshot-1",
        run_id="run-1",
        model_binding_id="binding-1",
        prompt_ref=ImmutableReference(uri="prompt://p1", hash=HASH),
        tools_ref=ImmutableReference(uri="tools://t1", hash=HASH),
        capability_requirements=["stream"] if stream else [],
        parameters={"temperature": 0.2},
        stream=stream,
        timeout_seconds=5,
        token_budget=None,
        cost_budget=None,
        idempotency_key="idempotency-123",
        authorization_token="platform-authorization-token",
    )


def model_route(provider: str, *, model: str = "integration-model") -> ModelRoute:
    return ModelRoute(
        provider=provider,
        model=model,
        base_url="https://provider.example/v1",
        secret_ref=f"secret://tenant/{TENANT_ID}/model/provider",
        capabilities=frozenset({"stream", "tools"}),
        timeout_seconds=5,
    )


class StaticBindingReader:
    def __init__(self, binding: ModelBinding) -> None:
        self.binding = binding

    async def get_binding(
        self, context: TenantContext, binding_id: str
    ) -> ModelBinding:
        assert context.tenant_id == str(TENANT_ID)
        assert binding_id == self.binding.binding_id
        return self.binding


class RecordingMaterializer:
    def __init__(self) -> None:
        self.calls = 0

    async def materialize(
        self, context: TenantContext, request: ModelGatewayRequest
    ) -> ModelInvocationInput:
        assert context.tenant_id == request.tenant_id
        self.calls += 1
        return ModelInvocationInput(
            messages=({"role": "user", "content": "materialized prompt"},),
            tools=(
                {
                    "type": "function",
                    "function": {
                        "name": "lookup_weather",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
            ),
        )


class RecordingOutputWriter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.outputs: list[JsonValue] = []

    async def write(
        self,
        context: TenantContext,
        request: ModelGatewayRequest,
        output: JsonValue,
    ) -> ImmutableReference:
        assert context.tenant_id == request.tenant_id
        self.outputs.append(output)
        if self.fail:
            raise RuntimeError("object storage unavailable")
        return ImmutableReference(uri="object://gateway/output-1", hash=HASH)


class RecordingUsage:
    def __init__(self) -> None:
        self.records: list[ModelUsageRecord] = []

    async def record(self, context: TenantContext, usage: ModelUsageRecord) -> None:
        assert context.tenant_id == str(usage.tenant_id)
        self.records.append(usage)


class StaticSecrets:
    async def resolve(self, context: TenantContext, secret_ref: str) -> SecretStr:
        assert context.tenant_id in secret_ref
        return SecretStr("provider-secret")


def gateway_service(
    *,
    binding: ModelBinding,
    materializer: RecordingMaterializer,
    output_writer: RecordingOutputWriter,
    usage: RecordingUsage,
    adapters: dict[str, ModelProviderAdapter],
) -> ModelGatewayService:
    return ModelGatewayService(
        bindings=StaticBindingReader(binding),
        materializer=materializer,
        output_writer=output_writer,
        secrets=StaticSecrets(),
        adapters=ProviderAdapterRegistry(adapters),
        usage_recorder=usage,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.provider)
async def test_gateway_generate_materializes_and_persists_provider_output(
    case: ProviderCase,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer provider-secret"
        assert all(
            "platform-authorization-token" not in value
            for value in request.headers.values()
        )
        body = cast(dict[str, object], json.loads(request.content))
        assert body["messages"] == [{"role": "user", "content": "materialized prompt"}]
        assert body["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "lookup_weather",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        return httpx.Response(
            200,
            headers={case.request_id_header: f"{case.provider}-header-request"},
            json={
                "id": f"{case.provider}-body-request",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"content": "provider output"},
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            },
        )

    materializer = RecordingMaterializer()
    output_writer = RecordingOutputWriter()
    usage = RecordingUsage()
    binding = ModelBinding(binding_id="binding-1", routes=(model_route(case.provider),))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = gateway_service(
            binding=binding,
            materializer=materializer,
            output_writer=output_writer,
            usage=usage,
            adapters={case.provider: case.factory(client)},
        )
        response = await service.generate(tenant_context(), gateway_request())

    assert materializer.calls == 1
    assert output_writer.outputs == [{"text": "provider output"}]
    assert response.output_ref.uri == "object://gateway/output-1"
    assert response.provider == case.provider
    assert response.provider_request_id == f"{case.provider}-body-request"
    assert len(usage.records) == 1
    assert usage.records[0].provider == case.provider
    assert usage.records[0].input_tokens == 5
    assert usage.records[0].output_tokens == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.provider)
async def test_gateway_stream_persists_terminal_provider_output(
    case: ProviderCase,
) -> None:
    stream_events: tuple[dict[str, object], ...] = (
        {
            "id": "stream-request",
            "choices": [{"index": 0, "delta": {"content": "hello"}}],
        },
        {
            "id": "stream-request",
            "choices": [],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
        {
            "id": "stream-request",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
    )
    stream_body = (
        "".join(f"data: {json.dumps(event)}\n\n" for event in stream_events)
        + "data: [DONE]\n\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        body = cast(dict[str, object], json.loads(request.content))
        assert body["stream"] is True
        return httpx.Response(
            200,
            headers={case.request_id_header: f"{case.provider}-stream-header"},
            text=stream_body,
        )

    materializer = RecordingMaterializer()
    output_writer = RecordingOutputWriter()
    usage = RecordingUsage()
    binding = ModelBinding(binding_id="binding-1", routes=(model_route(case.provider),))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = gateway_service(
            binding=binding,
            materializer=materializer,
            output_writer=output_writer,
            usage=usage,
            adapters={case.provider: case.factory(client)},
        )
        events = [
            event
            async for event in service.stream(
                tenant_context(), gateway_request(stream=True)
            )
        ]

    assert [event.event_type for event in events] == [
        "response_started",
        "text_delta",
        "usage",
        "response_completed",
    ]
    assert materializer.calls == 1
    assert output_writer.outputs == [{"text": "hello"}]
    completed = events[-1]
    assert isinstance(completed, ResponseCompletedEvent)
    assert completed.payload.output_ref.uri == "object://gateway/output-1"
    assert len(usage.records) == 1
    assert usage.records[0].provider == case.provider


@pytest.mark.asyncio
async def test_output_persistence_failure_never_falls_back_after_submission() -> None:
    requested_models: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = cast(dict[str, object], json.loads(request.content))
        requested_models.append(cast(str, body["model"]))
        return httpx.Response(
            200,
            json={
                "id": "submitted-request",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"content": "already generated"},
                    }
                ],
            },
        )

    output_writer = RecordingOutputWriter(fail=True)
    binding = ModelBinding(
        binding_id="binding-1",
        routes=(
            model_route("openai", model="openai-primary"),
            model_route("qwen", model="qwen-fallback"),
        ),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = gateway_service(
            binding=binding,
            materializer=RecordingMaterializer(),
            output_writer=output_writer,
            usage=RecordingUsage(),
            adapters={
                "openai": OpenAIProviderAdapter(client),
                "qwen": QwenProviderAdapter(client),
            },
        )
        with pytest.raises(ProviderError) as error:
            await service.generate(tenant_context(), gateway_request())

    assert error.value.code == "OUTPUT_PERSISTENCE_FAILED"
    assert error.value.submission_state == "submitted"
    assert requested_models == ["openai-primary"]
    assert output_writer.outputs == [{"text": "already generated"}]

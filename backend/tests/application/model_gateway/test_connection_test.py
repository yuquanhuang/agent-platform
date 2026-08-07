"""Connection-test event routing and terminal Operation contract tests."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from packages.application.model_gateway import (
    ModelProviderConnectionTestHandler,
    ProviderAdapterRegistry,
)
from packages.application.outbox import OutboxEventRouter, PermanentOutboxError
from packages.contracts.model_gateway import ModelGatewayRequest
from packages.contracts.public import TenantContext
from packages.domain.model_gateway import (
    AdapterResponse,
    AdapterStreamEvent,
    ModelInvocationInput,
    ModelRoute,
    ProviderConnectionTarget,
    ProviderError,
)
from packages.domain.outbox import OutboxEvent, OutboxStatus

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
OPERATION_ID = UUID("22222222-2222-4222-8222-222222222222")
PROVIDER_ID = UUID("33333333-3333-4333-8333-333333333333")
AGGREGATE_ID = PROVIDER_ID
NOW = datetime(2026, 8, 7, tzinfo=UTC)


def event(
    *, event_type: str = "model_provider.connection_test_requested"
) -> OutboxEvent:
    return OutboxEvent(
        id=uuid4(),
        tenant_id=TENANT_ID,
        aggregate_type="model_provider",
        aggregate_id=AGGREGATE_ID,
        event_type=event_type,
        payload={
            "operation_id": str(OPERATION_ID),
            "provider_id": str(PROVIDER_ID),
            "provider_type": "openai",
            "base_url": "https://provider.test/v1",
            "secret_ref": f"secret://tenant/{TENANT_ID}/model/provider",
            "timeout_seconds": 2,
        },
        payload_schema_version=1,
        status=OutboxStatus.PUBLISHING,
        attempts=1,
        next_attempt_at=NOW,
        created_at=NOW,
    )


class FakeSecrets:
    async def resolve(self, context: TenantContext, secret_ref: str) -> SecretStr:
        assert context.tenant_id in secret_ref
        return SecretStr("do-not-log")


class FakeAdapter:
    async def generate(
        self,
        route: ModelRoute,
        request: ModelGatewayRequest,
        invocation: ModelInvocationInput,
        credential: SecretStr,
    ) -> AdapterResponse:
        del route, request, invocation, credential
        raise AssertionError("not used")

    def stream(
        self,
        route: ModelRoute,
        request: ModelGatewayRequest,
        invocation: ModelInvocationInput,
        credential: SecretStr,
    ) -> AsyncIterator[AdapterStreamEvent]:
        del route, request, invocation, credential
        raise AssertionError("not used")

    async def test_connection(
        self, target: ProviderConnectionTarget, credential: SecretStr
    ) -> None:
        assert target.provider == "openai"
        assert credential.get_secret_value() == "do-not-log"


class RecordingOperations:
    def __init__(self) -> None:
        self.states: list[tuple[str, object]] = []

    async def mark_running(self, context: TenantContext, operation_id: UUID) -> None:
        self.states.append(("RUNNING", operation_id))

    async def mark_succeeded(
        self, context: TenantContext, operation_id: UUID, *, result: dict[str, object]
    ) -> None:
        self.states.append(("SUCCEEDED", result))

    async def mark_failed(
        self, context: TenantContext, operation_id: UUID, *, error: dict[str, object]
    ) -> None:
        self.states.append(("FAILED", error))


@pytest.mark.asyncio
async def test_connection_handler_terminalizes_operation_without_secret_echo() -> None:
    operations = RecordingOperations()
    handler = ModelProviderConnectionTestHandler(
        secrets=FakeSecrets(),
        adapters=ProviderAdapterRegistry({"openai": FakeAdapter()}),
        operations=operations,
    )

    result = await handler.start(event())

    assert result.workflow_id.startswith("model-provider-connection-test/")
    assert [state[0] for state in operations.states] == ["RUNNING", "SUCCEEDED"]
    assert "do-not-log" not in repr(operations.states)


@pytest.mark.asyncio
async def test_connection_handler_saves_safe_provider_error() -> None:
    class FailingAdapter(FakeAdapter):
        async def test_connection(
            self, target: ProviderConnectionTarget, credential: SecretStr
        ) -> None:
            del target, credential
            raise ProviderError(
                code="AUTHENTICATION_FAILED",
                message="safe authentication failure",
                retryable=False,
                submission_state="not_submitted",
            )

    operations = RecordingOperations()
    handler = ModelProviderConnectionTestHandler(
        secrets=FakeSecrets(),
        adapters=ProviderAdapterRegistry({"openai": FailingAdapter()}),
        operations=operations,
    )

    await handler.start(event())

    assert operations.states[-1][0] == "FAILED"
    assert operations.states[-1][1] == {
        "code": "AUTHENTICATION_FAILED",
        "message": "safe authentication failure",
        "retryable": False,
        "submission_state": "not_submitted",
    }


@pytest.mark.asyncio
async def test_router_keeps_probe_event_outside_model_connection_handler() -> None:
    handler = ModelProviderConnectionTestHandler(
        secrets=FakeSecrets(),
        adapters=ProviderAdapterRegistry({"openai": FakeAdapter()}),
        operations=RecordingOperations(),
    )
    router = OutboxEventRouter({"model_provider.connection_test_requested": handler})

    with pytest.raises(PermanentOutboxError):
        await router.start(event(event_type="platform_probe_requested.v1"))

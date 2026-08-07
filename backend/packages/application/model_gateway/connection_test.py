"""Model Provider connection-test Outbox handling."""

import asyncio
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from packages.application.model_gateway.gateway import (
    ProviderAdapterRegistry,
    SecretReferenceResolver,
)
from packages.application.outbox import PermanentOutboxError, WorkflowStartResult
from packages.contracts.public import SubjectType, TenantContext
from packages.domain.model_gateway import ProviderConnectionTarget, ProviderError
from packages.domain.outbox import OutboxEvent

MODEL_PROVIDER_CONNECTION_TEST_EVENT = "model_provider.connection_test_requested"


class ModelProviderConnectionTestPayloadV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    provider_id: UUID
    provider_type: Literal["openai", "qwen", "deepseek"]
    base_url: str
    secret_ref: str = Field(max_length=512)
    timeout_seconds: int = Field(ge=1, le=600)


class OperationCompletionStore(Protocol):
    async def mark_running(
        self, context: TenantContext, operation_id: UUID
    ) -> None: ...

    async def mark_succeeded(
        self,
        context: TenantContext,
        operation_id: UUID,
        *,
        result: dict[str, object],
    ) -> None: ...

    async def mark_failed(
        self,
        context: TenantContext,
        operation_id: UUID,
        *,
        error: dict[str, object],
    ) -> None: ...


class ModelProviderConnectionTestHandler:
    """Resolve a Secret only at the Gateway boundary and terminalize Operation."""

    def __init__(
        self,
        *,
        secrets: SecretReferenceResolver,
        adapters: ProviderAdapterRegistry,
        operations: OperationCompletionStore,
    ) -> None:
        self._secrets = secrets
        self._adapters = adapters
        self._operations = operations

    async def start(self, event: OutboxEvent) -> WorkflowStartResult:
        if event.event_type != MODEL_PROVIDER_CONNECTION_TEST_EVENT:
            raise PermanentOutboxError(f"unsupported event_type: {event.event_type}")
        if event.payload_schema_version != 1:
            raise PermanentOutboxError(
                "unsupported model provider connection-test payload version"
            )
        try:
            payload = ModelProviderConnectionTestPayloadV1.model_validate(event.payload)
        except ValueError as error:
            raise PermanentOutboxError(
                "invalid model provider connection-test payload"
            ) from error
        context = TenantContext(
            tenant_id=str(event.tenant_id),
            subject_type=SubjectType.SERVICE,
            subject_id=str(event.aggregate_id),
            auth_time=event.created_at,
            request_id=f"outbox:{event.id}",
            trace_id=f"outbox:{event.id}",
        )
        await self._operations.mark_running(context, payload.operation_id)
        target = ProviderConnectionTarget(
            provider_id=payload.provider_id,
            provider=payload.provider_type,
            base_url=payload.base_url,
            secret_ref=payload.secret_ref,
            timeout_seconds=payload.timeout_seconds,
        )
        try:
            credential = await self._secrets.resolve(context, target.secret_ref)
            adapter = self._adapters.get(target.provider)
            await asyncio.wait_for(
                adapter.test_connection(target, credential),
                timeout=target.timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except ProviderError as error:
            await self._operations.mark_failed(
                context,
                payload.operation_id,
                error=_safe_error(error),
            )
        except TimeoutError:
            await self._operations.mark_failed(
                context,
                payload.operation_id,
                error={
                    "code": "PROVIDER_TIMEOUT",
                    "message": "The model provider connection test timed out.",
                    "retryable": False,
                    "submission_state": "unknown",
                },
            )
        except (OSError, RuntimeError):
            await self._operations.mark_failed(
                context,
                payload.operation_id,
                error={
                    "code": "PROVIDER_UNAVAILABLE",
                    "message": "The model provider connection test failed.",
                    "retryable": False,
                    "submission_state": "unknown",
                },
            )
        else:
            await self._operations.mark_succeeded(
                context,
                payload.operation_id,
                result={
                    "provider_type": payload.provider_type,
                    "reachable": True,
                },
            )
        return WorkflowStartResult(
            workflow_id=f"model-provider-connection-test/{payload.operation_id}",
            run_id=None,
            already_exists=False,
        )


def _safe_error(error: ProviderError) -> dict[str, object]:
    return {
        "code": error.code,
        "message": error.safe_message,
        "retryable": error.retryable,
        "submission_state": error.submission_state,
    }

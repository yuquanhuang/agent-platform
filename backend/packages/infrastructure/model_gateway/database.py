"""PostgreSQL persistence for normalized usage and connection-test Operations."""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.outbox import PermanentOutboxError
from packages.contracts.model_gateway import Capability
from packages.contracts.public import TenantContext
from packages.domain.model_gateway import (
    ModelBinding,
    ModelParameterValue,
    ModelRoute,
    ModelUsageRecord,
    ProviderError,
)
from packages.infrastructure.database.models import (
    AuditLogModel,
    ModelBindingSnapshotModel,
    ModelUsageModel,
    OperationRecordModel,
)
from packages.infrastructure.database.uow import TenantUnitOfWork


class SqlAlchemyModelGatewayStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, context: TenantContext, usage: ModelUsageRecord) -> None:
        if str(usage.tenant_id) != context.tenant_id:
            raise ValueError("model usage tenant does not match TenantContext")
        if usage.finished_at < usage.started_at:
            raise ValueError("model usage finished_at precedes started_at")
        async with TenantUnitOfWork(self._session_factory, context) as uow:
            uow.session.add(
                ModelUsageModel(
                    id=usage.id,
                    tenant_id=usage.tenant_id,
                    run_id=usage.run_id,
                    provider=usage.provider,
                    model=usage.model,
                    provider_request_id=usage.provider_request_id,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    reasoning_tokens=usage.reasoning_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                    token_estimated=usage.token_estimated,
                    cost_amount=(
                        Decimal(usage.cost_amount)
                        if usage.cost_amount is not None
                        else None
                    ),
                    cost_currency=usage.cost_currency,
                    started_at=usage.started_at,
                    finished_at=usage.finished_at,
                )
            )

    async def mark_running(self, context: TenantContext, operation_id: UUID) -> None:
        async with TenantUnitOfWork(self._session_factory, context) as uow:
            operation = await _locked_operation(uow.session, context, operation_id)
            if operation.status == "ACCEPTED":
                operation.status = "RUNNING"
                operation.updated_at = datetime.now(UTC)

    async def mark_succeeded(
        self,
        context: TenantContext,
        operation_id: UUID,
        *,
        result: dict[str, object],
    ) -> None:
        await self._finish(
            context,
            operation_id,
            status="SUCCEEDED",
            result=result,
            error=None,
        )

    async def mark_failed(
        self,
        context: TenantContext,
        operation_id: UUID,
        *,
        error: dict[str, object],
    ) -> None:
        await self._finish(
            context,
            operation_id,
            status="FAILED",
            result=None,
            error=error,
        )

    async def _finish(
        self,
        context: TenantContext,
        operation_id: UUID,
        *,
        status: str,
        result: dict[str, object] | None,
        error: dict[str, object] | None,
    ) -> None:
        async with TenantUnitOfWork(self._session_factory, context) as uow:
            operation = await _locked_operation(uow.session, context, operation_id)
            if operation.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                return
            now = datetime.now(UTC)
            operation.status = status
            operation.result_json = result
            operation.error_json = error
            operation.updated_at = now
            operation.finished_at = now
            summary = result if result is not None else error or {}
            canonical = json.dumps(summary, sort_keys=True, separators=(",", ":"))
            uow.session.add(
                AuditLogModel(
                    tenant_id=UUID(context.tenant_id),
                    actor_type="service",
                    actor_id=UUID(context.subject_id),
                    action="model_provider.connection_test.completed",
                    resource_type="model_provider",
                    resource_id=operation.resource_id,
                    result="SUCCESS" if status == "SUCCEEDED" else "FAILED",
                    reason_codes=(
                        []
                        if error is None
                        else [str(error.get("code", "PROVIDER_ERROR"))]
                    ),
                    change_digest=(
                        f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
                    ),
                    request_id=context.request_id,
                    trace_id=context.trace_id,
                    metadata_json={
                        "operation_id": str(operation_id),
                        "status": status,
                    },
                )
            )


class SqlAlchemyModelBindingReader:
    """Resolve only immutable Model Config binding snapshots."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_binding(
        self, context: TenantContext, binding_id: str
    ) -> ModelBinding:
        try:
            model_config_version_id = UUID(binding_id)
        except ValueError as exc:
            raise _binding_not_found() from exc
        async with TenantUnitOfWork(self._session_factory, context) as uow:
            snapshot = await uow.session.scalar(
                select(ModelBindingSnapshotModel).where(
                    ModelBindingSnapshotModel.tenant_id == UUID(context.tenant_id),
                    ModelBindingSnapshotModel.model_config_version_id
                    == model_config_version_id,
                )
            )
            if snapshot is None:
                raise _binding_not_found()
            return ModelBinding(
                binding_id=str(snapshot.model_config_version_id),
                routes=(
                    ModelRoute(
                        provider=snapshot.provider_type,
                        model=snapshot.model_id,
                        base_url=snapshot.base_url,
                        secret_ref=snapshot.secret_ref,
                        capabilities=frozenset(
                            cast(list[Capability], snapshot.capabilities_json)
                        ),
                        timeout_seconds=snapshot.provider_timeout_seconds,
                        default_parameters=cast(
                            dict[str, ModelParameterValue],
                            snapshot.default_parameters_json,
                        ),
                        max_context_tokens=snapshot.max_context_tokens,
                        rate_limit_rpm=snapshot.rate_limit_rpm,
                    ),
                ),
            )


async def _locked_operation(
    session: AsyncSession, context: TenantContext, operation_id: UUID
) -> OperationRecordModel:
    operation = await session.scalar(
        select(OperationRecordModel)
        .where(
            OperationRecordModel.tenant_id == UUID(context.tenant_id),
            OperationRecordModel.id == operation_id,
            OperationRecordModel.operation_type == "model_provider.connection_test",
        )
        .with_for_update()
    )
    if operation is None:
        raise PermanentOutboxError("connection-test Operation is unavailable")
    return operation


def _binding_not_found() -> ProviderError:
    return ProviderError(
        code="MODEL_BINDING_NOT_FOUND",
        message="The immutable model binding is not available.",
        retryable=False,
        submission_state="not_submitted",
    )

"""PostgreSQL persistence for normalized usage and connection-test Operations."""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.outbox import PermanentOutboxError
from packages.contracts.model_gateway import Capability
from packages.contracts.public import TenantContext
from packages.domain.model_gateway import (
    ModelBinding,
    ModelParameterValue,
    ModelRoute,
    ModelUsageRecord,
    PriceCatalogRate,
    PriceDimension,
    ProviderError,
)
from packages.infrastructure.database.models import (
    AuditLogModel,
    ModelBindingSnapshotModel,
    ModelProviderAttemptModel,
    ModelUsageModel,
    OperationRecordModel,
    PriceCatalogRateModel,
    PriceCatalogVersionModel,
)
from packages.infrastructure.database.uow import TenantUnitOfWork
from packages.infrastructure.model_gateway.catalog import PriceCatalogVersion


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
                    cost_source=usage.cost_source,
                    price_catalog_version_id=usage.price_catalog_version_id,
                    cost_details_json=(
                        dict(usage.cost_details) if usage.cost_details else None
                    ),
                    started_at=usage.started_at,
                    finished_at=usage.finished_at,
                )
            )

    async def record_attempt(
        self,
        context: TenantContext,
        *,
        request: object,
        permit: object,
        route: ModelRoute,
        attempt_no: int,
        provider_request_id: str | None,
        submission_state: str,
        error_code: str | None,
        started_at: datetime,
        finished_at: datetime,
    ) -> None:
        from packages.application.model_gateway import BudgetPermit
        from packages.contracts.model_gateway import ModelGatewayRequest

        if not isinstance(request, ModelGatewayRequest) or not isinstance(
            permit, BudgetPermit
        ):
            raise TypeError("invalid provider attempt facts")
        async with TenantUnitOfWork(self._session_factory, context) as uow:
            await uow.session.execute(
                postgresql_insert(ModelProviderAttemptModel)
                .values(
                    id=uuid4(),
                    tenant_id=UUID(context.tenant_id),
                    reservation_id=permit.reservation_id,
                    run_id=request.run_id,
                    idempotency_key=request.idempotency_key,
                    attempt_no=attempt_no,
                    provider=route.provider,
                    model=route.model,
                    provider_request_id=provider_request_id,
                    submission_state=submission_state,
                    error_code=error_code,
                    started_at=started_at,
                    finished_at=finished_at,
                    created_at=finished_at,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        ModelProviderAttemptModel.tenant_id,
                        ModelProviderAttemptModel.run_id,
                        ModelProviderAttemptModel.idempotency_key,
                        ModelProviderAttemptModel.attempt_no,
                    ]
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


class SqlAlchemyPriceCatalogReader:
    """Read only tenant-scoped immutable catalog versions and rates."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_catalog(
        self,
        context: TenantContext,
        *,
        provider: str,
        model: str,
        occurred_at: datetime,
    ) -> PriceCatalogVersion | None:
        async with TenantUnitOfWork(self._session_factory, context) as uow:
            version = await uow.session.scalar(
                select(PriceCatalogVersionModel)
                .where(
                    PriceCatalogVersionModel.tenant_id == UUID(context.tenant_id),
                    PriceCatalogVersionModel.provider == provider,
                    PriceCatalogVersionModel.model == model,
                    PriceCatalogVersionModel.status == "PUBLISHED",
                    PriceCatalogVersionModel.effective_from <= occurred_at,
                    (
                        PriceCatalogVersionModel.effective_to.is_(None)
                        | (PriceCatalogVersionModel.effective_to > occurred_at)
                    ),
                )
                .order_by(PriceCatalogVersionModel.effective_from.desc())
                .limit(1)
            )
            if version is None:
                return None
            rates = (
                await uow.session.scalars(
                    select(PriceCatalogRateModel)
                    .where(
                        PriceCatalogRateModel.tenant_id == UUID(context.tenant_id),
                        PriceCatalogRateModel.catalog_version_id == version.id,
                    )
                    .order_by(PriceCatalogRateModel.dimension)
                )
            ).all()
            return PriceCatalogVersion(
                id=version.id,
                provider=version.provider,
                model=version.model,
                currency=version.currency,
                effective_from=version.effective_from,
                effective_to=version.effective_to,
                source_ref=version.source_ref,
                source_digest=version.source_digest,
                content_hash=version.content_hash,
                status=version.status,
                rates=tuple(
                    PriceCatalogRate(
                        id=rate.id,
                        dimension=cast(PriceDimension, rate.dimension),
                        unit_tokens=rate.unit_tokens,
                        unit_price=rate.unit_price,
                    )
                    for rate in rates
                ),
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
                        max_output_tokens=snapshot.max_output_tokens,
                        max_reasoning_tokens=snapshot.max_reasoning_tokens,
                        counter_profile_id=snapshot.counter_profile_id,
                        counter_profile_version=snapshot.counter_profile_version,
                        counter_profile_hash=snapshot.counter_profile_hash,
                        billing_semantics_version=snapshot.billing_semantics_version,
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

"""PostgreSQL admission policies for Model Gateway budgets and RPM limits."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.model_gateway import BudgetPermit
from packages.contracts.model_gateway import ModelGatewayRequest, ModelUsage
from packages.contracts.public import TenantContext
from packages.domain.model_gateway import ModelBinding, ModelRoute, ProviderError
from packages.infrastructure.database.models import (
    BudgetReservationModel,
    ModelRateLimitWindowModel,
    ModelUsageModel,
)
from packages.infrastructure.database.uow import TenantUnitOfWork

_MAX_BIGINT = 9_223_372_036_854_775_807
_RESERVATION_GRACE = timedelta(seconds=60)


class SqlAlchemyModelBudgetGuard:
    """Reserve the remaining Run token budget before provider submission."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    async def authorize(
        self,
        context: TenantContext,
        request: ModelGatewayRequest,
        binding: ModelBinding,
    ) -> BudgetPermit:
        if request.cost_budget is not None:
            raise _policy_error(
                "COST_BUDGET_UNAVAILABLE",
                "Cost budgets are unavailable until a trusted price table is configured.",
            )
        if request.token_budget is None:
            return BudgetPermit()
        if request.token_budget > _MAX_BIGINT:
            raise _policy_error(
                "INVALID_REQUEST",
                "The token budget exceeds the supported range.",
            )

        tenant_id = UUID(context.tenant_id)
        now = self._clock()
        async with TenantUnitOfWork(self._session_factory, context) as uow:
            await _lock_run_budget(uow.session, tenant_id, request.run_id)
            existing = await uow.session.scalar(
                select(BudgetReservationModel)
                .where(
                    BudgetReservationModel.tenant_id == tenant_id,
                    BudgetReservationModel.run_id == request.run_id,
                    BudgetReservationModel.idempotency_key == request.idempotency_key,
                )
                .with_for_update()
            )
            if existing is not None:
                if existing.status == "CONSUMED":
                    raise _policy_error(
                        "MODEL_REQUEST_ALREADY_COMPLETED",
                        "The model request idempotency key was already consumed.",
                    )
                if existing.status == "RESERVED" and existing.expires_at > now:
                    raise _policy_error(
                        "MODEL_REQUEST_IN_PROGRESS",
                        "The model request idempotency key is already in progress.",
                    )

            consumed_tokens = int(
                await uow.session.scalar(
                    select(
                        func.coalesce(
                            func.sum(
                                ModelUsageModel.input_tokens
                                + ModelUsageModel.output_tokens
                            ),
                            0,
                        )
                    ).where(
                        ModelUsageModel.tenant_id == tenant_id,
                        ModelUsageModel.run_id == request.run_id,
                    )
                )
                or 0
            )
            reserved_tokens = int(
                await uow.session.scalar(
                    select(
                        func.coalesce(
                            func.sum(BudgetReservationModel.reserved_tokens), 0
                        )
                    ).where(
                        BudgetReservationModel.tenant_id == tenant_id,
                        BudgetReservationModel.run_id == request.run_id,
                        BudgetReservationModel.status == "RESERVED",
                        BudgetReservationModel.expires_at > now,
                    )
                )
                or 0
            )
            remaining_tokens = request.token_budget - consumed_tokens - reserved_tokens
            if remaining_tokens < 1:
                raise _policy_error(
                    "TOKEN_BUDGET_EXCEEDED",
                    "The Run token budget has been exhausted.",
                )

            expires_at = (
                now + timedelta(seconds=request.timeout_seconds) + _RESERVATION_GRACE
            )
            if existing is None:
                existing = BudgetReservationModel(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    run_id=request.run_id,
                    user_id=request.user_id,
                    agent_id=request.agent_id,
                    model_binding_id=binding.binding_id,
                    idempotency_key=request.idempotency_key,
                    reserved_tokens=remaining_tokens,
                    consumed_tokens=None,
                    status="RESERVED",
                    expires_at=expires_at,
                    created_at=now,
                    updated_at=now,
                    finished_at=None,
                )
                uow.session.add(existing)
            else:
                existing.user_id = request.user_id
                existing.agent_id = request.agent_id
                existing.model_binding_id = binding.binding_id
                existing.reserved_tokens = remaining_tokens
                existing.consumed_tokens = None
                existing.status = "RESERVED"
                existing.expires_at = expires_at
                existing.updated_at = now
                existing.finished_at = None
            await uow.session.flush()
            return BudgetPermit(
                reservation_id=existing.id,
                reserved_tokens=remaining_tokens,
            )

    async def settle(
        self,
        context: TenantContext,
        request: ModelGatewayRequest,
        permit: BudgetPermit,
        usage: ModelUsage,
    ) -> ProviderError | None:
        if permit.reservation_id is None:
            return None
        actual_tokens = usage.input_tokens + usage.output_tokens
        now = self._clock()
        async with TenantUnitOfWork(self._session_factory, context) as uow:
            reservation = await uow.session.scalar(
                select(BudgetReservationModel)
                .where(
                    BudgetReservationModel.tenant_id == UUID(context.tenant_id),
                    BudgetReservationModel.id == permit.reservation_id,
                    BudgetReservationModel.run_id == request.run_id,
                    BudgetReservationModel.idempotency_key == request.idempotency_key,
                )
                .with_for_update()
            )
            if reservation is None:
                return _submitted_policy_error(
                    "BUDGET_RESERVATION_NOT_FOUND",
                    "The token budget reservation is unavailable.",
                )
            if reservation.status == "CONSUMED":
                if reservation.consumed_tokens == actual_tokens:
                    return None
                return _submitted_policy_error(
                    "BUDGET_RESERVATION_MISMATCH",
                    "The token budget reservation was settled with different usage.",
                )
            if reservation.status == "RELEASED":
                return _submitted_policy_error(
                    "BUDGET_RESERVATION_RELEASED",
                    "The token budget reservation was already released.",
                )

            reservation.status = "CONSUMED"
            reservation.consumed_tokens = actual_tokens
            reservation.updated_at = now
            reservation.finished_at = now
            if actual_tokens > reservation.reserved_tokens:
                return _submitted_policy_error(
                    "TOKEN_BUDGET_EXCEEDED",
                    "The provider usage exceeded the reserved Run token budget.",
                )
            return None

    async def release(self, context: TenantContext, permit: BudgetPermit) -> None:
        if permit.reservation_id is None:
            return
        now = self._clock()
        async with TenantUnitOfWork(self._session_factory, context) as uow:
            reservation = await uow.session.scalar(
                select(BudgetReservationModel)
                .where(
                    BudgetReservationModel.tenant_id == UUID(context.tenant_id),
                    BudgetReservationModel.id == permit.reservation_id,
                )
                .with_for_update()
            )
            if reservation is not None and reservation.status == "RESERVED":
                reservation.status = "RELEASED"
                reservation.updated_at = now
                reservation.finished_at = now


class SqlAlchemyModelRateLimiter:
    """Increment a tenant/binding/provider fixed UTC minute counter atomically."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    async def acquire(
        self,
        context: TenantContext,
        request: ModelGatewayRequest,
        binding: ModelBinding,
        route: ModelRoute,
    ) -> None:
        del request
        limit = route.rate_limit_rpm
        if limit is None:
            return
        if limit < 1:
            raise _policy_error(
                "INVALID_MODEL_BINDING",
                "The model binding contains an invalid RPM limit.",
            )

        now = self._clock()
        window_started_at = now.replace(second=0, microsecond=0)
        statement = (
            postgresql_insert(ModelRateLimitWindowModel)
            .values(
                tenant_id=UUID(context.tenant_id),
                model_binding_id=binding.binding_id,
                provider=route.provider,
                window_started_at=window_started_at,
                request_count=1,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[
                    ModelRateLimitWindowModel.tenant_id,
                    ModelRateLimitWindowModel.model_binding_id,
                    ModelRateLimitWindowModel.provider,
                    ModelRateLimitWindowModel.window_started_at,
                ],
                set_={
                    "request_count": ModelRateLimitWindowModel.request_count + 1,
                    "updated_at": now,
                },
                where=ModelRateLimitWindowModel.request_count < limit,
            )
            .returning(ModelRateLimitWindowModel.request_count)
        )
        async with TenantUnitOfWork(self._session_factory, context) as uow:
            request_count = (await uow.session.execute(statement)).scalar_one_or_none()
            if request_count is None:
                raise ProviderError(
                    code="GATEWAY_RATE_LIMITED",
                    message="The local model request rate limit was exceeded.",
                    retryable=True,
                    submission_state="not_submitted",
                )


async def _lock_run_budget(session: AsyncSession, tenant_id: UUID, run_id: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": f"model-budget:{tenant_id}:{run_id}"},
    )


def _policy_error(code: str, message: str) -> ProviderError:
    return ProviderError(
        code=code,
        message=message,
        retryable=False,
        submission_state="not_submitted",
    )


def _submitted_policy_error(code: str, message: str) -> ProviderError:
    return ProviderError(
        code=code,
        message=message,
        retryable=False,
        submission_state="submitted",
    )

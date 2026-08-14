"""PostgreSQL admission policies for Model Gateway budgets and RPM limits."""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.model_gateway import BudgetPermit
from packages.application.model_gateway.cost_budget import CostBoundPlanner
from packages.contracts.model_gateway import ModelGatewayRequest, ModelUsage
from packages.contracts.public import TenantContext
from packages.domain.model_gateway import (
    ModelBinding,
    ModelInvocationInput,
    ModelRoute,
    ProviderError,
)
from packages.infrastructure.database.models import (
    AuditLogModel,
    BudgetPolicyModel,
    BudgetPolicyVersionModel,
    BudgetReservationModel,
    CostLedgerEntryModel,
    ModelRateLimitWindowModel,
    ModelUsageModel,
    OutboxEventModel,
    TenantModel,
)
from packages.infrastructure.database.uow import TenantUnitOfWork

_MAX_BIGINT = 9_223_372_036_854_775_807
_RESERVATION_GRACE = timedelta(seconds=60)


class SqlAlchemyModelBudgetGuard:
    """Atomically reserve Run and active tenant-period token budgets."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        cost_planner: CostBoundPlanner | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._cost_planner = cost_planner
        self._clock = clock or (lambda: datetime.now(UTC))

    async def authorize(
        self,
        context: TenantContext,
        request: ModelGatewayRequest,
        binding: ModelBinding,
        invocation: ModelInvocationInput | None = None,
    ) -> BudgetPermit:
        if request.token_budget is not None and request.token_budget > _MAX_BIGINT:
            raise _policy_error(
                "INVALID_REQUEST",
                "The token budget exceeds the supported range.",
            )

        tenant_id = UUID(context.tenant_id)
        now = self._clock()
        cost_plan = None
        if request.cost_budget is not None:
            if self._cost_planner is None or invocation is None:
                raise _policy_error(
                    "COST_BOUND_UNAVAILABLE",
                    "A trusted model cost upper bound is unavailable.",
                )
            cost_plan = await self._cost_planner.plan(
                context,
                binding=binding,
                invocation=invocation,
                currency=request.cost_budget.currency,
                occurred_at=now,
            )
            if Decimal(cost_plan.amount.amount) > Decimal(request.cost_budget.amount):
                raise _policy_error(
                    "COST_BUDGET_EXCEEDED",
                    "The request cost budget is smaller than its trusted upper bound.",
                )
        async with TenantUnitOfWork(self._session_factory, context) as uow:
            policy = await _load_active_budget_policy(uow.session, tenant_id)
            period = (
                _period_bounds(now, policy[1].period) if policy is not None else None
            )
            if cost_plan is not None and (policy is None or period is None):
                raise _policy_error(
                    "COST_BUDGET_UNAVAILABLE",
                    "An active tenant-period cost policy is required.",
                )
            lock_keys: list[str] = []
            if request.token_budget is not None:
                lock_keys.append(f"model-budget:{tenant_id}:{request.run_id}")
            if policy is not None and period is not None:
                lock_keys.append(f"model-budget-policy:{tenant_id}:{policy[0].id}")
            if cost_plan is not None:
                if request.cost_budget is None:
                    raise RuntimeError("cost plan is missing its request currency")
                lock_keys.append(
                    f"model-cost-budget:{tenant_id}:{request.cost_budget.currency}"
                )
            for lock_key in sorted(lock_keys):
                await _lock_budget(uow.session, lock_key)
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
                if (
                    existing.status == "RESERVED"
                    and existing.reserved_cost_amount is not None
                ):
                    _add_cost_ledger(
                        uow.session,
                        reservation=existing,
                        entry_type="RELEASE",
                        amount=existing.reserved_cost_amount,
                        currency=existing.reserved_cost_currency,
                        details={"reason": "expired_reservation_replaced"},
                        now=now,
                    )

            run_remaining = None
            if request.token_budget is not None:
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
                run_remaining = request.token_budget - consumed_tokens - reserved_tokens

            policy_remaining = None
            policy_id = None
            policy_version_id = None
            period_started_at = None
            period_ends_at = None
            if policy is not None and period is not None:
                policy_model, version = policy
                policy_id = policy_model.id
                policy_version_id = version.id
                period_started_at, period_ends_at = period
                period_consumed = int(
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
                            ModelUsageModel.finished_at >= period_started_at,
                            ModelUsageModel.finished_at < period_ends_at,
                        )
                    )
                    or 0
                )
                period_reserved = int(
                    await uow.session.scalar(
                        select(
                            func.coalesce(
                                func.sum(BudgetReservationModel.reserved_tokens), 0
                            )
                        ).where(
                            BudgetReservationModel.tenant_id == tenant_id,
                            BudgetReservationModel.budget_policy_id == policy_id,
                            BudgetReservationModel.budget_period_started_at
                            == period_started_at,
                            BudgetReservationModel.status == "RESERVED",
                            BudgetReservationModel.expires_at > now,
                        )
                    )
                    or 0
                )
                policy_remaining = (
                    version.token_limit - period_consumed - period_reserved
                )
                if cost_plan is not None:
                    if version.cost_limit_amount is None:
                        raise _policy_error(
                            "COST_BUDGET_UNAVAILABLE",
                            "The active tenant policy has no cost limit.",
                        )
                    if version.cost_limit_currency != cost_plan.amount.currency:
                        raise _policy_error(
                            "COST_CURRENCY_MISMATCH",
                            "The request, policy and price catalog currencies must match.",
                        )
                    reserved_amount = Decimal(
                        await uow.session.scalar(
                            select(
                                func.coalesce(func.sum(CostLedgerEntryModel.amount), 0)
                            ).where(
                                CostLedgerEntryModel.tenant_id == tenant_id,
                                CostLedgerEntryModel.period_started_at
                                == period_started_at,
                                CostLedgerEntryModel.currency
                                == cost_plan.amount.currency,
                                CostLedgerEntryModel.entry_type == "RESERVE",
                            )
                        )
                        or 0
                    ) - Decimal(
                        await uow.session.scalar(
                            select(
                                func.coalesce(func.sum(CostLedgerEntryModel.amount), 0)
                            ).where(
                                CostLedgerEntryModel.tenant_id == tenant_id,
                                CostLedgerEntryModel.period_started_at
                                == period_started_at,
                                CostLedgerEntryModel.currency
                                == cost_plan.amount.currency,
                                CostLedgerEntryModel.entry_type.in_(
                                    ("RELEASE", "SETTLE")
                                ),
                            )
                        )
                        or 0
                    )
                    settled_amount = Decimal(
                        await uow.session.scalar(
                            select(
                                func.coalesce(func.sum(CostLedgerEntryModel.amount), 0)
                            ).where(
                                CostLedgerEntryModel.tenant_id == tenant_id,
                                CostLedgerEntryModel.period_started_at
                                == period_started_at,
                                CostLedgerEntryModel.currency
                                == cost_plan.amount.currency,
                                CostLedgerEntryModel.entry_type.in_(
                                    ("ADJUST", "UNKNOWN")
                                ),
                            )
                        )
                        or 0
                    )
                    ledger_amount = reserved_amount + settled_amount
                    if ledger_amount + Decimal(cost_plan.amount.amount) > Decimal(
                        version.cost_limit_amount
                    ):
                        if version.enforcement == "HARD":
                            raise _policy_error(
                                "COST_BUDGET_EXCEEDED",
                                "The tenant-period cost budget has been exhausted.",
                            )
                        await _add_soft_threshold_event(
                            uow.session,
                            context=context,
                            policy=policy_model,
                            version=version,
                            period_started_at=period_started_at,
                            currency=cost_plan.amount.currency,
                            observed=ledger_amount + Decimal(cost_plan.amount.amount),
                            limit=Decimal(version.cost_limit_amount),
                            now=now,
                        )

            remaining_candidates = [
                value
                for value in (run_remaining, policy_remaining)
                if value is not None
            ]
            if not remaining_candidates and cost_plan is None:
                return BudgetPermit()
            remaining_tokens = min(remaining_candidates) if remaining_candidates else 1
            if remaining_candidates and remaining_tokens < 1:
                if run_remaining is not None and run_remaining < 1:
                    raise _policy_error(
                        "TOKEN_BUDGET_EXCEEDED",
                        "The Run token budget has been exhausted.",
                    )
                if (
                    policy is not None
                    and policy[1].enforcement == "SOFT"
                    and policy_remaining is not None
                    and policy_remaining < 1
                ):
                    remaining_tokens = max(1, request.token_budget or 1)
                    await _add_soft_threshold_event(
                        uow.session,
                        context=context,
                        policy=policy[0],
                        version=policy[1],
                        period_started_at=period_started_at,
                        dimension="tokens",
                        observed=Decimal(policy[1].token_limit + 1),
                        limit=Decimal(policy[1].token_limit),
                        currency=None,
                        now=now,
                    )
                else:
                    if run_remaining is not None and policy_remaining is not None:
                        scope = "Run or tenant-period"
                    elif run_remaining is not None:
                        scope = "Run"
                    else:
                        scope = "Tenant-period"
                    raise _policy_error(
                        "TOKEN_BUDGET_EXCEEDED",
                        f"The {scope} token budget has been exhausted.",
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
                    budget_policy_id=policy_id,
                    budget_policy_version_id=policy_version_id,
                    budget_period_started_at=period_started_at,
                    budget_period_ends_at=period_ends_at,
                    reserved_cost_amount=(
                        Decimal(cost_plan.amount.amount)
                        if cost_plan is not None
                        else None
                    ),
                    reserved_cost_currency=(
                        cost_plan.amount.currency if cost_plan is not None else None
                    ),
                    canonical_input_hash=(
                        cost_plan.counted_input.canonical_input_hash
                        if cost_plan is not None
                        else None
                    ),
                    counter_profile_id=(
                        cost_plan.counted_input.counter_profile_id
                        if cost_plan is not None
                        else None
                    ),
                    counter_profile_version=(
                        cost_plan.counted_input.counter_profile_version
                        if cost_plan is not None
                        else None
                    ),
                    counter_profile_hash=(
                        cost_plan.counted_input.counter_profile_hash
                        if cost_plan is not None
                        else None
                    ),
                    upper_bound_json=(
                        _cost_plan_json(cost_plan) if cost_plan is not None else None
                    ),
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
                existing.budget_policy_id = policy_id
                existing.budget_policy_version_id = policy_version_id
                existing.budget_period_started_at = period_started_at
                existing.budget_period_ends_at = period_ends_at
                existing.reserved_cost_amount = (
                    Decimal(cost_plan.amount.amount) if cost_plan is not None else None
                )
                existing.reserved_cost_currency = (
                    cost_plan.amount.currency if cost_plan is not None else None
                )
                existing.canonical_input_hash = (
                    cost_plan.counted_input.canonical_input_hash
                    if cost_plan is not None
                    else None
                )
                existing.counter_profile_id = (
                    cost_plan.counted_input.counter_profile_id
                    if cost_plan is not None
                    else None
                )
                existing.counter_profile_version = (
                    cost_plan.counted_input.counter_profile_version
                    if cost_plan is not None
                    else None
                )
                existing.counter_profile_hash = (
                    cost_plan.counted_input.counter_profile_hash
                    if cost_plan is not None
                    else None
                )
                existing.upper_bound_json = (
                    _cost_plan_json(cost_plan) if cost_plan is not None else None
                )
            await uow.session.flush()
            if cost_plan is not None and period is not None:
                _add_cost_ledger(
                    uow.session,
                    reservation=existing,
                    entry_type="RESERVE",
                    amount=Decimal(cost_plan.amount.amount),
                    currency=cost_plan.amount.currency,
                    details=_cost_plan_json(cost_plan),
                    now=now,
                )
            return BudgetPermit(
                reservation_id=existing.id,
                reserved_tokens=remaining_tokens,
                reserved_cost_amount=(
                    cost_plan.amount.amount if cost_plan is not None else None
                ),
                reserved_cost_currency=(
                    cost_plan.amount.currency if cost_plan is not None else None
                ),
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
            if reservation.reserved_cost_amount is not None:
                if usage.cost is None:
                    _add_cost_ledger(
                        uow.session,
                        reservation=reservation,
                        entry_type="UNKNOWN",
                        amount=Decimal(0),
                        currency=reservation.reserved_cost_currency,
                        details={"reason": "provider_cost_unavailable"},
                        now=now,
                    )
                else:
                    if usage.cost.currency != reservation.reserved_cost_currency:
                        _add_cost_ledger(
                            uow.session,
                            reservation=reservation,
                            entry_type="UNKNOWN",
                            amount=Decimal(0),
                            currency=reservation.reserved_cost_currency,
                            details={
                                "reason": "provider_cost_currency_mismatch",
                                "provider_currency": usage.cost.currency,
                            },
                            now=now,
                        )
                        return _submitted_policy_error(
                            "COST_CURRENCY_MISMATCH",
                            "The provider cost currency differs from the reservation.",
                        )
                    actual_cost = Decimal(usage.cost.amount)
                    _add_cost_ledger(
                        uow.session,
                        reservation=reservation,
                        entry_type="SETTLE",
                        amount=reservation.reserved_cost_amount,
                        currency=reservation.reserved_cost_currency,
                        details={"actual_amount": format(actual_cost, "f")},
                        now=now,
                    )
                    _add_cost_ledger(
                        uow.session,
                        reservation=reservation,
                        entry_type="ADJUST",
                        amount=actual_cost,
                        currency=reservation.reserved_cost_currency,
                        details={"reason": "actual_cost"},
                        now=now,
                    )
                    if actual_cost > reservation.reserved_cost_amount:
                        return _submitted_policy_error(
                            "COST_BUDGET_EXCEEDED",
                            "The provider cost exceeded its trusted upper bound.",
                        )
            if actual_tokens > reservation.reserved_tokens:
                return _submitted_policy_error(
                    "TOKEN_BUDGET_EXCEEDED",
                    "The provider usage exceeded the reserved token budget.",
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
                if reservation.reserved_cost_amount is not None:
                    _add_cost_ledger(
                        uow.session,
                        reservation=reservation,
                        entry_type="RELEASE",
                        amount=reservation.reserved_cost_amount,
                        currency=reservation.reserved_cost_currency,
                        details={"reason": "not_submitted"},
                        now=now,
                    )


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


async def _lock_budget(session: AsyncSession, lock_key: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )


async def _load_active_budget_policy(
    session: AsyncSession, tenant_id: UUID
) -> tuple[BudgetPolicyModel, BudgetPolicyVersionModel] | None:
    statement = (
        select(BudgetPolicyModel, BudgetPolicyVersionModel)
        .join(
            BudgetPolicyVersionModel,
            (BudgetPolicyVersionModel.tenant_id == BudgetPolicyModel.tenant_id)
            & (BudgetPolicyVersionModel.policy_id == BudgetPolicyModel.id)
            & (BudgetPolicyVersionModel.id == BudgetPolicyModel.current_version_id),
        )
        .join(TenantModel, TenantModel.budget_policy_id == BudgetPolicyModel.id)
        .where(
            TenantModel.id == tenant_id,
            BudgetPolicyModel.tenant_id == tenant_id,
            BudgetPolicyModel.status == "ACTIVE",
        )
        .with_for_update(read=True, of=BudgetPolicyModel)
    )
    row = (await session.execute(statement)).one_or_none()
    return (row[0], row[1]) if row is not None else None


def _period_bounds(now: datetime, period: str) -> tuple[datetime, datetime]:
    utc_now = now.astimezone(UTC)
    start = utc_now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "DAILY":
        return start, start + timedelta(days=1)
    if period == "MONTHLY":
        if start.month == 12:
            next_start = start.replace(year=start.year + 1, month=1, day=1)
        else:
            next_start = start.replace(month=start.month + 1, day=1)
        return start.replace(day=1), next_start
    raise _policy_error("INVALID_BUDGET_POLICY", "The budget policy period is invalid.")


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


def _cost_plan_json(plan: object) -> dict[str, object]:
    from packages.application.model_gateway.cost_budget import CostBoundPlan

    if not isinstance(plan, CostBoundPlan):
        raise TypeError("invalid cost bound plan")
    return {
        "amount": plan.amount.amount,
        "currency": plan.amount.currency,
        "canonical_input_hash": plan.counted_input.canonical_input_hash,
        "counter_profile_id": plan.counted_input.counter_profile_id,
        "counter_profile_version": plan.counted_input.counter_profile_version,
        "counter_profile_hash": plan.counted_input.counter_profile_hash,
        "routes": [
            {
                "route_index": route.route_index,
                "provider": route.provider,
                "model": route.model,
                "catalog_version_id": str(route.catalog_version_id),
                "amount": route.amount.amount,
            }
            for route in plan.routes
        ],
    }


def _add_cost_ledger(
    session: AsyncSession,
    *,
    reservation: BudgetReservationModel,
    entry_type: str,
    amount: Decimal,
    currency: str | None,
    details: dict[str, object],
    now: datetime,
) -> None:
    if currency is None:
        raise RuntimeError("cost ledger currency is unavailable")
    if (
        reservation.budget_period_started_at is None
        or reservation.budget_period_ends_at is None
    ):
        raise RuntimeError("cost ledger period is unavailable")
    canonical = json.dumps(details, sort_keys=True, separators=(",", ":"))
    session.add(
        CostLedgerEntryModel(
            id=uuid4(),
            tenant_id=reservation.tenant_id,
            reservation_id=reservation.id,
            budget_policy_id=reservation.budget_policy_id,
            budget_policy_version_id=reservation.budget_policy_version_id,
            price_catalog_version_id=None,
            entry_type=entry_type,
            amount=amount,
            currency=currency,
            period_started_at=reservation.budget_period_started_at,
            period_ends_at=reservation.budget_period_ends_at,
            run_id=reservation.run_id,
            user_id=reservation.user_id,
            agent_id=reservation.agent_id,
            model_binding_id=reservation.model_binding_id,
            provider=None,
            model=None,
            provider_attempt_no=None,
            details_json=details,
            entry_hash="sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
            created_at=now,
        )
    )


async def _add_soft_threshold_event(
    session: AsyncSession,
    *,
    context: TenantContext,
    policy: BudgetPolicyModel,
    version: BudgetPolicyVersionModel,
    period_started_at: datetime | None,
    dimension: str = "cost",
    currency: str | None,
    observed: Decimal,
    limit: Decimal,
    now: datetime,
) -> None:
    if period_started_at is None:
        raise RuntimeError("SOFT threshold period is unavailable")
    event_id = uuid5(
        NAMESPACE_URL,
        ":".join(
            (
                "budget-soft",
                str(policy.tenant_id),
                str(version.id),
                period_started_at.isoformat(),
                dimension,
                currency or "tokens",
            )
        ),
    )
    payload = {
        "policy_id": str(policy.id),
        "policy_version_id": str(version.id),
        "period_started_at": period_started_at.isoformat(),
        "dimension": dimension,
        "currency": currency,
        "observed": format(observed, "f"),
        "limit": format(limit, "f"),
    }
    inserted = (
        await session.execute(
            postgresql_insert(OutboxEventModel)
            .values(
                id=event_id,
                tenant_id=policy.tenant_id,
                aggregate_type="BudgetPolicy",
                aggregate_id=policy.id,
                event_type="budget.threshold_crossed.v1",
                payload_json=payload,
                payload_schema_version=1,
                status="PENDING",
                attempts=0,
                next_attempt_at=now,
                created_at=now,
                published_at=None,
            )
            .on_conflict_do_nothing(index_elements=[OutboxEventModel.id])
            .returning(OutboxEventModel.id)
        )
    ).scalar_one_or_none()
    if inserted is None:
        return
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    session.add(
        AuditLogModel(
            tenant_id=policy.tenant_id,
            actor_type="service",
            actor_id=UUID(context.subject_id),
            action="budget.threshold_crossed",
            resource_type="budget_policy",
            resource_id=policy.id,
            run_id=None,
            result="SUCCESS",
            reason_codes=["SOFT_BUDGET_LIMIT_EXCEEDED"],
            change_digest="sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
            request_id=context.request_id,
            trace_id=context.trace_id,
            metadata_json=payload,
            created_at=now,
        )
    )

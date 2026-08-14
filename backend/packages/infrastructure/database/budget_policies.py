"""PostgreSQL persistence for durable tenant periodic model budgets."""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.metadata import RequestMetadata
from packages.application.policy.budgets import BudgetPolicyStore
from packages.contracts.generated.resources_models import (
    ActionRequest,
    BudgetPolicyCreateRequest,
    BudgetPolicyCreateRequestCostLimitChoice2,
    BudgetPolicyUpdateRequest,
    BudgetPolicyUpdateRequestCostLimitChoice2,
)
from packages.contracts.public import (
    TenantContext,
    resource_state_conflict,
    resource_version_conflict,
    validation_error,
)
from packages.domain.public import (
    BudgetEnforcement,
    BudgetPeriod,
    BudgetPolicyRecord,
    BudgetPolicyStatus,
    BudgetPolicyVersionRecord,
    MutationOutcome,
    decode_cursor,
    encode_cursor,
    format_etag,
)
from packages.infrastructure.database.idempotency import (
    claim_idempotency as _claim_idempotency,
)
from packages.infrastructure.database.idempotency import (
    complete_idempotency as _complete_idempotency,
)
from packages.infrastructure.database.models import (
    AuditLogModel,
    BudgetPolicyModel,
    BudgetPolicyVersionModel,
    TenantModel,
)
from packages.infrastructure.database.uow import TenantUnitOfWork


class SqlAlchemyBudgetPolicyStore(BudgetPolicyStore):
    """Own tenant transactions for one selected immutable budget version."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_policies(
        self, context: TenantContext, *, limit: int, cursor: str | None
    ) -> tuple[list[BudgetPolicyRecord], str | None]:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as uow:
            statement = _joined_statement(tenant_id).order_by(
                BudgetPolicyModel.created_at.desc(), BudgetPolicyModel.id.desc()
            )
            if cursor is not None:
                created_at, policy_id = _decode_cursor(cursor)
                statement = statement.where(
                    or_(
                        BudgetPolicyModel.created_at < created_at,
                        and_(
                            BudgetPolicyModel.created_at == created_at,
                            BudgetPolicyModel.id < policy_id,
                        ),
                    )
                )
            rows = list((await uow.session.execute(statement.limit(limit + 1))).all())
            page_rows = rows[:limit]
            next_cursor = (
                encode_cursor(page_rows[-1][0].created_at, page_rows[-1][0].id)
                if len(rows) > limit and page_rows
                else None
            )
            return [_policy_record(*row) for row in page_rows], next_cursor

    async def create_policy(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        request: BudgetPolicyCreateRequest,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[BudgetPolicyRecord]:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as uow:
            session = uow.session
            record_id, replay = await _claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type="budget_policy.create",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            existing_policy_id = await session.scalar(
                select(BudgetPolicyModel.id).where(
                    BudgetPolicyModel.tenant_id == tenant_id
                )
            )
            if existing_policy_id is not None:
                raise resource_state_conflict(
                    "The tenant already has a BudgetPolicy; update its immutable version."
                )
            policy_id = uuid4()
            version_id = uuid4()
            now = datetime.now(UTC)
            version = _new_version(
                version_id=version_id,
                tenant_id=tenant_id,
                policy_id=policy_id,
                version_no=1,
                period=request.period,
                token_limit=request.token_limit,
                enforcement=request.enforcement or "HARD",
                cost_limit=request.cost_limit,
                actor_id=actor_id,
                now=now,
            )
            policy = BudgetPolicyModel(
                id=policy_id,
                tenant_id=tenant_id,
                name=request.name,
                description=request.description,
                status="ACTIVE",
                current_version_id=version_id,
                resource_version=1,
                created_by=actor_id,
                created_at=now,
                updated_at=now,
            )
            session.add_all((policy, version))
            tenant = await session.scalar(
                select(TenantModel).where(TenantModel.id == tenant_id).with_for_update()
            )
            if tenant is None:
                raise resource_state_conflict("The active tenant is unavailable.")
            tenant.budget_policy_id = policy_id
            tenant.resource_version += 1
            tenant.updated_at = now
            await session.flush()
            result = _policy_record(policy, version)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="resource.create",
                resource_id=policy_id,
                metadata=metadata,
                change=_version_change(
                    version, fields=["name", "period", "token_limit"]
                ),
            )
            await _complete_idempotency(
                session,
                record_id,
                response_status=201,
                response_body=_policy_json(result),
                response_etag=format_etag(result.resource_version),
                response_ref=str(result.id),
            )
            return MutationOutcome(value=result)

    async def get_policy(
        self, context: TenantContext, policy_id: UUID
    ) -> BudgetPolicyRecord | None:
        async with TenantUnitOfWork(self._session_factory, context) as uow:
            row = (
                await uow.session.execute(
                    _joined_statement(UUID(context.tenant_id), policy_id)
                )
            ).one_or_none()
            return _policy_record(*row) if row is not None else None

    async def update_policy(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        policy_id: UUID,
        expected_version: int,
        request: BudgetPolicyUpdateRequest,
        metadata: RequestMetadata,
    ) -> BudgetPolicyRecord | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as uow:
            session = uow.session
            policy = await session.scalar(
                select(BudgetPolicyModel)
                .where(
                    BudgetPolicyModel.tenant_id == tenant_id,
                    BudgetPolicyModel.id == policy_id,
                )
                .with_for_update()
            )
            if policy is None:
                return None
            _check_version(policy.resource_version, expected_version)
            current = await _current_version(session, tenant_id, policy)
            if request.name is not None:
                policy.name = request.name
            if "description" in request.model_fields_set:
                policy.description = request.description
            version_fields = {"period", "enforcement", "token_limit", "cost_limit"}
            next_version = current
            if request.model_fields_set & version_fields:
                next_version = _new_version(
                    version_id=uuid4(),
                    tenant_id=tenant_id,
                    policy_id=policy_id,
                    version_no=current.version_no + 1,
                    period=(
                        request.period
                        if request.period is not None
                        else cast(BudgetPeriod, current.period)
                    ),
                    token_limit=(
                        request.token_limit
                        if request.token_limit is not None
                        else current.token_limit
                    ),
                    enforcement=(
                        request.enforcement
                        if request.enforcement is not None
                        else cast(BudgetEnforcement, current.enforcement)
                    ),
                    cost_limit=(
                        request.cost_limit
                        if "cost_limit" in request.model_fields_set
                        else _money(current)
                    ),
                    actor_id=actor_id,
                    now=datetime.now(UTC),
                )
                session.add(next_version)
                policy.current_version_id = next_version.id
            policy.resource_version += 1
            policy.updated_at = datetime.now(UTC)
            await session.flush()
            result = _policy_record(policy, next_version)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="resource.update",
                resource_id=policy_id,
                metadata=metadata,
                change=_version_change(
                    next_version, fields=sorted(request.model_fields_set)
                ),
            )
            return result

    async def set_policy_status(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        policy_id: UUID,
        expected_version: int,
        enabled: bool,
        request: ActionRequest | None,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[BudgetPolicyRecord] | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as uow:
            session = uow.session
            operation = "enable" if enabled else "disable"
            record_id, replay = await _claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type=f"budget_policy.{operation}",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            policy = await session.scalar(
                select(BudgetPolicyModel)
                .where(
                    BudgetPolicyModel.tenant_id == tenant_id,
                    BudgetPolicyModel.id == policy_id,
                )
                .with_for_update()
            )
            if policy is None:
                return None
            _check_version(policy.resource_version, expected_version)
            target = "ACTIVE" if enabled else "DISABLED"
            if policy.status == target:
                raise resource_state_conflict(
                    f"BudgetPolicy is already {target.lower()}."
                )
            version = await _current_version(session, tenant_id, policy)
            policy.status = target
            policy.resource_version += 1
            policy.updated_at = datetime.now(UTC)
            await session.flush()
            result = _policy_record(policy, version)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action=f"resource.{operation}",
                resource_id=policy_id,
                metadata=metadata,
                change={
                    "status": target,
                    "reason_present": bool(request and request.reason),
                },
            )
            await _complete_idempotency(
                session,
                record_id,
                response_status=200,
                response_body=_policy_json(result),
                response_etag=format_etag(result.resource_version),
                response_ref=str(result.id),
            )
            return MutationOutcome(value=result)

    async def list_versions(
        self,
        context: TenantContext,
        *,
        policy_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[BudgetPolicyVersionRecord], str | None] | None:
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as uow:
            session = uow.session
            existing_policy_id = await session.scalar(
                select(BudgetPolicyModel.id).where(
                    BudgetPolicyModel.tenant_id == tenant_id,
                    BudgetPolicyModel.id == policy_id,
                )
            )
            if existing_policy_id is None:
                return None
            statement = (
                select(BudgetPolicyVersionModel)
                .where(
                    BudgetPolicyVersionModel.tenant_id == tenant_id,
                    BudgetPolicyVersionModel.policy_id == policy_id,
                )
                .order_by(
                    BudgetPolicyVersionModel.created_at.desc(),
                    BudgetPolicyVersionModel.id.desc(),
                )
            )
            if cursor is not None:
                created_at, version_id = _decode_cursor(cursor)
                statement = statement.where(
                    or_(
                        BudgetPolicyVersionModel.created_at < created_at,
                        and_(
                            BudgetPolicyVersionModel.created_at == created_at,
                            BudgetPolicyVersionModel.id < version_id,
                        ),
                    )
                )
            rows = list(await session.scalars(statement.limit(limit + 1)))
            page_rows = rows[:limit]
            next_cursor = (
                encode_cursor(page_rows[-1].created_at, page_rows[-1].id)
                if len(rows) > limit and page_rows
                else None
            )
            return [_version_record(row) for row in page_rows], next_cursor


def _joined_statement(
    tenant_id: UUID, policy_id: UUID | None = None
):  # type: ignore[no-untyped-def]
    statement = (
        select(BudgetPolicyModel, BudgetPolicyVersionModel)
        .join(
            BudgetPolicyVersionModel,
            and_(
                BudgetPolicyVersionModel.tenant_id == BudgetPolicyModel.tenant_id,
                BudgetPolicyVersionModel.policy_id == BudgetPolicyModel.id,
                BudgetPolicyVersionModel.id == BudgetPolicyModel.current_version_id,
            ),
        )
        .where(BudgetPolicyModel.tenant_id == tenant_id)
    )
    return (
        statement.where(BudgetPolicyModel.id == policy_id)
        if policy_id is not None
        else statement
    )


async def _current_version(
    session: AsyncSession, tenant_id: UUID, policy: BudgetPolicyModel
) -> BudgetPolicyVersionModel:
    version = await session.scalar(
        select(BudgetPolicyVersionModel).where(
            BudgetPolicyVersionModel.tenant_id == tenant_id,
            BudgetPolicyVersionModel.policy_id == policy.id,
            BudgetPolicyVersionModel.id == policy.current_version_id,
        )
    )
    if version is None:
        raise RuntimeError("BudgetPolicy current version is unavailable")
    return version


def _new_version(
    *,
    version_id: UUID,
    tenant_id: UUID,
    policy_id: UUID,
    version_no: int,
    period: BudgetPeriod,
    token_limit: int,
    enforcement: BudgetEnforcement,
    cost_limit: (
        BudgetPolicyCreateRequestCostLimitChoice2
        | BudgetPolicyUpdateRequestCostLimitChoice2
        | None
    ),
    actor_id: UUID,
    now: datetime,
) -> BudgetPolicyVersionModel:
    money = cost_limit
    amount = Decimal(money.amount) if money is not None else None
    currency = money.currency if money is not None else None
    if currency is not None and currency not in {"USD", "CNY"}:
        raise validation_error("Budget cost currency must be USD or CNY.")
    values: dict[str, str | int | None] = {
        "period": period,
        "enforcement": enforcement,
        "token_limit": token_limit,
        "cost_limit_amount": format(amount, "f") if amount is not None else None,
        "cost_limit_currency": currency,
    }
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return BudgetPolicyVersionModel(
        id=version_id,
        tenant_id=tenant_id,
        policy_id=policy_id,
        version_no=version_no,
        period=period,
        enforcement=enforcement,
        token_limit=token_limit,
        cost_limit_amount=amount,
        cost_limit_currency=currency,
        price_catalog_version=None,
        content_hash=f"sha256:{hashlib.sha256(canonical).hexdigest()}",
        created_by=actor_id,
        created_at=now,
    )


def _policy_record(
    policy: BudgetPolicyModel, version: BudgetPolicyVersionModel
) -> BudgetPolicyRecord:
    return BudgetPolicyRecord(
        id=policy.id,
        tenant_id=policy.tenant_id,
        name=policy.name,
        description=policy.description,
        status=cast(BudgetPolicyStatus, policy.status),
        current_version=_version_record(version),
        resource_version=policy.resource_version,
        created_at=policy.created_at,
        updated_at=policy.updated_at,
    )


def _version_record(model: BudgetPolicyVersionModel) -> BudgetPolicyVersionRecord:
    return BudgetPolicyVersionRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        policy_id=model.policy_id,
        version_no=model.version_no,
        period=cast(BudgetPeriod, model.period),
        enforcement=cast(BudgetEnforcement, model.enforcement),
        token_limit=model.token_limit,
        cost_limit_amount=model.cost_limit_amount,
        cost_limit_currency=model.cost_limit_currency,
        price_catalog_version=model.price_catalog_version,
        content_hash=model.content_hash,
        created_by=model.created_by,
        created_at=model.created_at,
    )


def _policy_json(record: BudgetPolicyRecord) -> dict[str, object]:
    version = record.current_version
    return {
        "id": str(record.id),
        "tenant_id": str(record.tenant_id),
        "name": record.name,
        "description": record.description,
        "status": record.status,
        "current_version": {
            "id": str(version.id),
            "policy_id": str(version.policy_id),
            "version_no": version.version_no,
            "period": version.period,
            "enforcement": version.enforcement,
            "token_limit": version.token_limit,
            "cost_limit": (
                {
                    "amount": format(version.cost_limit_amount, "f"),
                    "currency": version.cost_limit_currency,
                }
                if version.cost_limit_amount is not None
                else None
            ),
            "price_catalog_version": version.price_catalog_version,
            "content_hash": version.content_hash,
            "created_by": str(version.created_by),
            "created_at": version.created_at.isoformat(),
        },
        "resource_version": record.resource_version,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def _version_change(
    version: BudgetPolicyVersionModel, *, fields: list[str]
) -> dict[str, object]:
    return {
        "fields": fields,
        "version_no": version.version_no,
        "period": version.period,
        "enforcement": version.enforcement,
        "token_limit": version.token_limit,
        "cost_limit": (
            {
                "amount": format(version.cost_limit_amount, "f"),
                "currency": version.cost_limit_currency,
            }
            if version.cost_limit_amount is not None
            else None
        ),
        "content_hash": version.content_hash,
    }


def _check_version(current: int, expected: int) -> None:
    if current != expected:
        raise resource_version_conflict()


def _money(
    version: BudgetPolicyVersionModel,
) -> BudgetPolicyUpdateRequestCostLimitChoice2 | None:
    if version.cost_limit_amount is None or version.cost_limit_currency is None:
        return None
    return BudgetPolicyUpdateRequestCostLimitChoice2(
        amount=format(version.cost_limit_amount, "f"),
        currency=cast(Literal["USD", "CNY"], version.cost_limit_currency),
    )


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        return decode_cursor(cursor)
    except ValueError as exc:
        raise validation_error("Pagination cursor is invalid.") from exc


async def _add_audit(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_id: UUID,
    action: str,
    resource_id: UUID,
    metadata: RequestMetadata,
    change: dict[str, object],
) -> None:
    canonical = json.dumps(change, sort_keys=True, separators=(",", ":")).encode()
    session.add(
        AuditLogModel(
            tenant_id=tenant_id,
            actor_type="user",
            actor_id=actor_id,
            action=action,
            resource_type="budget_policy",
            resource_id=resource_id,
            result="SUCCESS",
            reason_codes=[],
            change_digest=f"sha256:{hashlib.sha256(canonical).hexdigest()}",
            request_id=metadata.request_id,
            trace_id=metadata.trace_id,
            metadata_json=change,
        )
    )

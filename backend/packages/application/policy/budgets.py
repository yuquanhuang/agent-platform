"""Tenant BudgetPolicy management and immutable version mapping."""

from collections.abc import Callable
from typing import Literal, Protocol, cast
from uuid import UUID

from pydantic import BaseModel

from packages.application.metadata import RequestMetadata
from packages.application.resources import TenantAccessResolver, canonical_request_hash
from packages.contracts.generated.resources_models import (
    ActionRequest,
    BudgetPolicy,
    BudgetPolicyCreateRequest,
    BudgetPolicyPage,
    BudgetPolicyUpdateRequest,
    BudgetPolicyVersion,
    BudgetPolicyVersionCostLimitChoice2,
    BudgetPolicyVersionPage,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    TenantContext,
    permission_denied,
    resource_not_found,
    validation_error,
)
from packages.domain.public import (
    BudgetPolicyRecord,
    BudgetPolicyVersionRecord,
    MutationOutcome,
    TenantAccess,
    format_etag,
    parse_etag,
)


class BudgetPolicyStore(Protocol):
    async def list_policies(
        self, context: TenantContext, *, limit: int, cursor: str | None
    ) -> tuple[list[BudgetPolicyRecord], str | None]: ...

    async def create_policy(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        request: BudgetPolicyCreateRequest,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[BudgetPolicyRecord]: ...

    async def get_policy(
        self, context: TenantContext, policy_id: UUID
    ) -> BudgetPolicyRecord | None: ...

    async def update_policy(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        policy_id: UUID,
        expected_version: int,
        request: BudgetPolicyUpdateRequest,
        metadata: RequestMetadata,
    ) -> BudgetPolicyRecord | None: ...

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
    ) -> MutationOutcome[BudgetPolicyRecord] | None: ...

    async def list_versions(
        self,
        context: TenantContext,
        *,
        policy_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[BudgetPolicyVersionRecord], str | None] | None: ...


class BudgetPolicyManagementService:
    """Authorize tenant periodic model token budget operations."""

    def __init__(
        self, access_resolver: TenantAccessResolver, store: BudgetPolicyStore
    ) -> None:
        self._access_resolver = access_resolver
        self._store = store

    async def list_policies(
        self,
        principal: AuthenticatedPrincipal,
        *,
        limit: int,
        cursor: str | None,
        metadata: RequestMetadata,
    ) -> BudgetPolicyPage:
        access = await self._access(principal, "list", metadata)
        records, next_cursor = await self._store.list_policies(
            access.context, limit=limit, cursor=cursor
        )
        return BudgetPolicyPage(
            items=[_policy(record) for record in records],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    async def create_policy(
        self,
        principal: AuthenticatedPrincipal,
        *,
        request: BudgetPolicyCreateRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> tuple[BudgetPolicy, str]:
        access = await self._access(principal, "create", metadata)
        outcome = await self._store.create_policy(
            access.context,
            actor_id=UUID(access.context.subject_id),
            request=request,
            idempotency_key=idempotency_key,
            request_hash=_request_hash("budget_policy.create", request),
            metadata=metadata,
        )
        return _versioned_outcome(outcome, BudgetPolicy, _policy)

    async def get_policy(
        self,
        principal: AuthenticatedPrincipal,
        *,
        policy_id: str,
        metadata: RequestMetadata,
    ) -> tuple[BudgetPolicy, str]:
        access = await self._access(principal, "read", metadata)
        record = await self._store.get_policy(access.context, _resource_id(policy_id))
        if record is None:
            raise resource_not_found()
        return _policy(record), format_etag(record.resource_version)

    async def update_policy(
        self,
        principal: AuthenticatedPrincipal,
        *,
        policy_id: str,
        if_match: str,
        request: BudgetPolicyUpdateRequest,
        metadata: RequestMetadata,
    ) -> tuple[BudgetPolicy, str]:
        if not request.model_fields_set:
            raise validation_error("BudgetPolicy update requires at least one field.")
        access = await self._access(principal, "update", metadata)
        record = await self._store.update_policy(
            access.context,
            actor_id=UUID(access.context.subject_id),
            policy_id=_resource_id(policy_id),
            expected_version=_expected_version(if_match),
            request=request,
            metadata=metadata,
        )
        if record is None:
            raise resource_not_found()
        return _policy(record), format_etag(record.resource_version)

    async def set_policy_enabled(
        self,
        principal: AuthenticatedPrincipal,
        *,
        policy_id: str,
        if_match: str,
        enabled: bool,
        request: ActionRequest | None,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> tuple[BudgetPolicy, str]:
        access = await self._access(principal, "disable", metadata)
        operation = "enable" if enabled else "disable"
        outcome = await self._store.set_policy_status(
            access.context,
            actor_id=UUID(access.context.subject_id),
            policy_id=_resource_id(policy_id),
            expected_version=_expected_version(if_match),
            enabled=enabled,
            request=request,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                f"budget_policy.{operation}",
                request,
                extra={"policy_id": policy_id, "if_match": if_match},
            ),
            metadata=metadata,
        )
        if outcome is None:
            raise resource_not_found()
        return _versioned_outcome(outcome, BudgetPolicy, _policy)

    async def list_versions(
        self,
        principal: AuthenticatedPrincipal,
        *,
        policy_id: str,
        limit: int,
        cursor: str | None,
        metadata: RequestMetadata,
    ) -> BudgetPolicyVersionPage:
        access = await self._access(principal, "read", metadata)
        result = await self._store.list_versions(
            access.context,
            policy_id=_resource_id(policy_id),
            limit=limit,
            cursor=cursor,
        )
        if result is None:
            raise resource_not_found()
        records, next_cursor = result
        return BudgetPolicyVersionPage(
            items=[_version(record) for record in records],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    async def _access(
        self,
        principal: AuthenticatedPrincipal,
        action: str,
        metadata: RequestMetadata,
    ) -> TenantAccess:
        access = await self._access_resolver.resolve_tenant_access(principal, metadata)
        if not access.allows("budget_policy", action):
            raise permission_denied()
        return access


def _policy(record: BudgetPolicyRecord) -> BudgetPolicy:
    return BudgetPolicy(
        id=str(record.id),
        tenant_id=str(record.tenant_id),
        name=record.name,
        description=record.description,
        status=record.status,
        current_version=_version(record.current_version),
        resource_version=record.resource_version,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _version(record: BudgetPolicyVersionRecord) -> BudgetPolicyVersion:
    return BudgetPolicyVersion(
        id=str(record.id),
        policy_id=str(record.policy_id),
        version_no=record.version_no,
        period=record.period,
        enforcement=record.enforcement,
        token_limit=record.token_limit,
        cost_limit=(
            BudgetPolicyVersionCostLimitChoice2(
                amount=format(record.cost_limit_amount, "f"),
                currency=cast(Literal["USD", "CNY"], record.cost_limit_currency),
            )
            if record.cost_limit_amount is not None
            and record.cost_limit_currency is not None
            else None
        ),
        price_catalog_version=record.price_catalog_version,
        content_hash=record.content_hash,
        created_by=str(record.created_by),
        created_at=record.created_at,
    )


def _resource_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise validation_error("Resource identifier is invalid.") from exc


def _expected_version(value: str) -> int:
    try:
        return parse_etag(value)
    except ValueError as exc:
        raise validation_error("If-Match header is invalid.") from exc


def _request_hash(
    operation: str,
    request: BaseModel | None,
    *,
    extra: dict[str, object] | None = None,
) -> str:
    return canonical_request_hash(operation, request, extra=extra)


class VersionedRecord(Protocol):
    @property
    def resource_version(self) -> int: ...


def _versioned_outcome[RecordT: VersionedRecord, ModelT: BaseModel](
    outcome: MutationOutcome[RecordT],
    model_type: type[ModelT],
    mapper: Callable[[RecordT], ModelT],
) -> tuple[ModelT, str]:
    if outcome.replay is not None:
        if outcome.replay.response_etag is None:
            raise RuntimeError("versioned replay is missing an ETag")
        return (
            model_type.model_validate(outcome.replay.response_body),
            outcome.replay.response_etag,
        )
    if outcome.value is None:
        raise RuntimeError("mutation outcome is missing its value")
    return mapper(outcome.value), format_etag(outcome.value.resource_version)

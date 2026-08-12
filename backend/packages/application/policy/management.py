"""Tenant QuotaPolicy management and immutable version mapping."""

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel

from packages.application.metadata import RequestMetadata
from packages.application.policy.admission import RunCapacityPolicy
from packages.application.resources import TenantAccessResolver, canonical_request_hash
from packages.contracts.generated.resources_models import (
    ActionRequest,
    QuotaPolicy,
    QuotaPolicyCreateRequest,
    QuotaPolicyPage,
    QuotaPolicyUpdateRequest,
    QuotaPolicyVersion,
    QuotaPolicyVersionPage,
    RunCapacityLimits,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    TenantContext,
    permission_denied,
    resource_not_found,
    validation_error,
)
from packages.domain.public import (
    MutationOutcome,
    QuotaPolicyRecord,
    QuotaPolicyVersionRecord,
    RunCapacityLimitsRecord,
    TenantAccess,
    format_etag,
    parse_etag,
)


class QuotaPolicyStore(Protocol):
    async def list_policies(
        self, context: TenantContext, *, limit: int, cursor: str | None
    ) -> tuple[list[QuotaPolicyRecord], str | None]: ...

    async def create_policy(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        request: QuotaPolicyCreateRequest,
        limits: RunCapacityPolicy,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[QuotaPolicyRecord]: ...

    async def get_policy(
        self, context: TenantContext, policy_id: UUID
    ) -> QuotaPolicyRecord | None: ...

    async def update_policy(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        policy_id: UUID,
        expected_version: int,
        request: QuotaPolicyUpdateRequest,
        limits: RunCapacityPolicy | None,
        metadata: RequestMetadata,
    ) -> QuotaPolicyRecord | None: ...

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
    ) -> MutationOutcome[QuotaPolicyRecord] | None: ...

    async def list_versions(
        self,
        context: TenantContext,
        *,
        policy_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[QuotaPolicyVersionRecord], str | None] | None: ...


class QuotaPolicyManagementService:
    """Authorize and validate tenant Run capacity policy operations."""

    def __init__(
        self,
        access_resolver: TenantAccessResolver,
        store: QuotaPolicyStore,
        deployment_policy: RunCapacityPolicy,
    ) -> None:
        self._access_resolver = access_resolver
        self._store = store
        self._deployment_policy = deployment_policy

    async def list_policies(
        self,
        principal: AuthenticatedPrincipal,
        *,
        limit: int,
        cursor: str | None,
        metadata: RequestMetadata,
    ) -> QuotaPolicyPage:
        access = await self._access(principal, "list", metadata)
        records, next_cursor = await self._store.list_policies(
            access.context, limit=limit, cursor=cursor
        )
        return QuotaPolicyPage(
            items=[_policy(record) for record in records],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    async def create_policy(
        self,
        principal: AuthenticatedPrincipal,
        *,
        request: QuotaPolicyCreateRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> tuple[QuotaPolicy, str]:
        access = await self._access(principal, "create", metadata)
        limits = self._validated_limits(request.limits)
        outcome = await self._store.create_policy(
            access.context,
            actor_id=UUID(access.context.subject_id),
            request=request,
            limits=limits,
            idempotency_key=idempotency_key,
            request_hash=_request_hash("quota_policy.create", request),
            metadata=metadata,
        )
        return _versioned_outcome(outcome, QuotaPolicy, _policy)

    async def get_policy(
        self,
        principal: AuthenticatedPrincipal,
        *,
        policy_id: str,
        metadata: RequestMetadata,
    ) -> tuple[QuotaPolicy, str]:
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
        request: QuotaPolicyUpdateRequest,
        metadata: RequestMetadata,
    ) -> tuple[QuotaPolicy, str]:
        if not request.model_fields_set:
            raise validation_error("QuotaPolicy update requires at least one field.")
        limits = (
            self._validated_limits(request.limits)
            if request.limits is not None
            else None
        )
        access = await self._access(principal, "update", metadata)
        record = await self._store.update_policy(
            access.context,
            actor_id=UUID(access.context.subject_id),
            policy_id=_resource_id(policy_id),
            expected_version=_expected_version(if_match),
            request=request,
            limits=limits,
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
    ) -> tuple[QuotaPolicy, str]:
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
                f"quota_policy.{operation}",
                request,
                extra={"policy_id": policy_id, "if_match": if_match},
            ),
            metadata=metadata,
        )
        if outcome is None:
            raise resource_not_found()
        return _versioned_outcome(outcome, QuotaPolicy, _policy)

    async def list_versions(
        self,
        principal: AuthenticatedPrincipal,
        *,
        policy_id: str,
        limit: int,
        cursor: str | None,
        metadata: RequestMetadata,
    ) -> QuotaPolicyVersionPage:
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
        return QuotaPolicyVersionPage(
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
        if not access.allows("quota_policy", action):
            raise permission_denied()
        return access

    def _validated_limits(self, value: RunCapacityLimits) -> RunCapacityPolicy:
        if not value.model_fields_set:
            raise validation_error("QuotaPolicy limits require at least one dimension.")
        policy = RunCapacityPolicy(**value.model_dump())
        expansion_fields = self._deployment_policy.expansion_fields(policy)
        if expansion_fields:
            raise validation_error(
                "QuotaPolicy cannot exceed deployment hard limits: "
                + ", ".join(expansion_fields)
            )
        return policy


def _policy(record: QuotaPolicyRecord) -> QuotaPolicy:
    return QuotaPolicy(
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


def _version(record: QuotaPolicyVersionRecord) -> QuotaPolicyVersion:
    return QuotaPolicyVersion(
        id=str(record.id),
        policy_id=str(record.policy_id),
        version_no=record.version_no,
        limits=RunCapacityLimits(**_limits_json(record.limits)),
        content_hash=record.content_hash,
        created_by=str(record.created_by),
        created_at=record.created_at,
    )


def _limits_json(record: RunCapacityLimitsRecord) -> dict[str, int]:
    return {
        field_name: value
        for field_name in (
            "max_nonterminal_runs_per_tenant",
            "max_nonterminal_runs_per_user",
            "max_nonterminal_runs_per_agent",
            "max_nonterminal_agentscope_runs",
            "max_nonterminal_codex_runs",
        )
        if (value := getattr(record, field_name)) is not None
    }


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
    result = mapper(outcome.value)
    resource_version = outcome.value.resource_version
    return result, format_etag(resource_version)

"""Frozen publishAgent/getRelease application boundary."""

from typing import Protocol
from uuid import UUID

from packages.application.metadata import RequestMetadata
from packages.application.resources.hashing import canonical_request_hash
from packages.contracts.generated.core_models import (
    Error,
    PublishAgentRequest,
    Release,
    ReleaseAccepted,
    RollbackAgentRequest,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    TenantContext,
    permission_denied,
    resource_not_found,
    validation_error,
)
from packages.domain.public import MutationOutcome, ReleaseRecord, TenantAccess

RELEASE_REQUESTED_EVENT = "agent.release_requested.v1"


def publish_workflow_id(tenant_id: UUID, release_id: UUID) -> str:
    return f"publish/{tenant_id}/{release_id}"


class ReleaseAccessResolver(Protocol):
    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess: ...


class ReleaseStore(Protocol):
    async def request_release(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        agent_id: UUID,
        expected_agent_version: int,
        runtime_targets: tuple[str, ...],
        release_note: str,
        run_smoke_test: bool,
        activate_on_success: bool,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[ReleaseRecord] | None: ...

    async def request_rollback(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        agent_id: UUID,
        snapshot_id: UUID,
        runtime_targets: tuple[str, ...],
        release_note: str,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[ReleaseRecord] | None: ...

    async def get_release(
        self, context: TenantContext, *, release_id: UUID
    ) -> ReleaseRecord | None: ...


class ReleaseManagementService:
    """Authorize and persist one asynchronous publication request."""

    def __init__(
        self, access_resolver: ReleaseAccessResolver, store: ReleaseStore
    ) -> None:
        self._access_resolver = access_resolver
        self._store = store

    async def publish_agent(
        self,
        principal: AuthenticatedPrincipal,
        *,
        agent_id: str,
        request: PublishAgentRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> ReleaseAccepted:
        access = await self._access_resolver.resolve_tenant_access(principal, metadata)
        if not access.allows("agent", "publish"):
            raise permission_denied()
        parsed_agent_id = _uuid(agent_id, "agent_id")
        runtime_targets = normalize_runtime_targets(request.runtime_targets)
        run_smoke_test = request.run_smoke_test is not False
        activate_on_success = request.activate_on_success is not False
        outcome = await self._store.request_release(
            access.context,
            actor_id=UUID(access.context.subject_id),
            agent_id=parsed_agent_id,
            expected_agent_version=request.expected_agent_version,
            runtime_targets=runtime_targets,
            release_note=request.release_note,
            run_smoke_test=run_smoke_test,
            activate_on_success=activate_on_success,
            idempotency_key=idempotency_key,
            request_hash=canonical_request_hash(
                "agent.release.request",
                request,
                extra={"agent_id": str(parsed_agent_id)},
                unordered_fields=("runtime_targets",),
            ),
            metadata=metadata,
        )
        if outcome is None:
            raise resource_not_found()
        if outcome.replay is not None:
            return ReleaseAccepted.model_validate(outcome.replay.response_body)
        if outcome.value is None:
            raise RuntimeError("Release request outcome is missing its value")
        return _accepted(outcome.value)

    async def rollback_agent(
        self,
        principal: AuthenticatedPrincipal,
        *,
        agent_id: str,
        request: RollbackAgentRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> ReleaseAccepted:
        access = await self._access_resolver.resolve_tenant_access(principal, metadata)
        if not access.allows("agent", "publish"):
            raise permission_denied()
        parsed_agent_id = _uuid(agent_id, "agent_id")
        parsed_snapshot_id = _uuid(request.snapshot_id, "snapshot_id")
        runtime_targets = normalize_runtime_targets(request.runtime_targets)
        outcome = await self._store.request_rollback(
            access.context,
            actor_id=UUID(access.context.subject_id),
            agent_id=parsed_agent_id,
            snapshot_id=parsed_snapshot_id,
            runtime_targets=runtime_targets,
            release_note=request.release_note,
            idempotency_key=idempotency_key,
            request_hash=canonical_request_hash(
                "agent.rollback.request",
                request,
                extra={"agent_id": str(parsed_agent_id)},
                unordered_fields=("runtime_targets",),
            ),
            metadata=metadata,
        )
        if outcome is None:
            raise resource_not_found()
        if outcome.replay is not None:
            return ReleaseAccepted.model_validate(outcome.replay.response_body)
        if outcome.value is None:
            raise RuntimeError("Rollback request outcome is missing its value")
        return _accepted(outcome.value)

    async def get_release(
        self,
        principal: AuthenticatedPrincipal,
        *,
        release_id: str,
        metadata: RequestMetadata,
    ) -> Release:
        access = await self._access_resolver.resolve_tenant_access(principal, metadata)
        if not access.allows("agent", "read"):
            raise permission_denied()
        record = await self._store.get_release(
            access.context, release_id=_uuid(release_id, "release_id")
        )
        if record is None:
            raise resource_not_found()
        error: Error | None = None
        if record.error_code is not None:
            detail = record.error_detail or {}
            message = detail.get("message")
            error = Error(
                code=record.error_code,
                message=(
                    str(message)
                    if isinstance(message, str)
                    else "The Release workflow failed."
                ),
                request_id=metadata.request_id,
                retryable=False,
                details=detail or None,
            )
        return Release(
            id=str(record.id),
            agent_id=str(record.agent_id),
            status=record.status,
            workflow_id=record.workflow_id,
            snapshot_id=(str(record.snapshot_id) if record.snapshot_id else None),
            deployment_ids=[str(item) for item in record.deployment_ids],
            error=error,
            created_at=record.created_at,
        )


def _accepted(record: ReleaseRecord) -> ReleaseAccepted:
    return ReleaseAccepted(
        release_id=str(record.id),
        workflow_id=record.workflow_id,
        status="REQUESTED",
        status_url=f"/api/v1/releases/{record.id}",
    )


def normalize_runtime_targets(values: list[str]) -> tuple[str, ...]:
    normalized = tuple(sorted({value.strip() for value in values if value.strip()}))
    if not normalized or len(normalized) != len(values):
        raise validation_error("runtime_targets must contain unique non-empty values.")
    if len(normalized) > 32 or any(len(value) > 255 for value in normalized):
        raise validation_error(
            "runtime_targets exceeds the supported publication limit."
        )
    return normalized


def _uuid(value: str, field: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise validation_error(f"{field} must be a valid resource identifier.") from exc

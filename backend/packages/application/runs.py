"""Run creation, cancellation, retry and query use cases."""

import hashlib
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from packages.application.metadata import RequestMetadata
from packages.application.resources.hashing import canonical_request_hash
from packages.contracts.generated.core_models import (
    CancelRunRequest,
    Error,
    RetryRunRequest,
    Run,
    RunAccepted,
    RunCreateRequest,
    RunPage,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    TenantContext,
    permission_denied,
    resource_not_found,
    validation_error,
)
from packages.contracts.temporal import CancelRunSignal
from packages.domain.public import MutationOutcome, RunRecord, TenantAccess

RUN_REQUESTED_EVENT = "agent.run_requested.v1"


class RunAccessResolver(Protocol):
    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess: ...


class RunStore(Protocol):
    async def list_session_runs(
        self,
        context: TenantContext,
        *,
        user_id: UUID,
        session_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[RunRecord], str | None] | None: ...

    async def create_run(
        self,
        context: TenantContext,
        *,
        user_id: UUID,
        request: RunCreateRequest,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[RunRecord]: ...

    async def get_run(
        self, context: TenantContext, *, user_id: UUID, run_id: UUID
    ) -> RunRecord | None: ...

    async def request_cancel(
        self,
        context: TenantContext,
        *,
        user_id: UUID,
        run_id: UUID,
        request: CancelRunRequest,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[RunRecord] | None: ...

    async def retry_run(
        self,
        context: TenantContext,
        *,
        user_id: UUID,
        run_id: UUID,
        request: RetryRunRequest,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[RunRecord] | None: ...


class RunWorkflowControl(Protocol):
    async def signal_cancel(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        signal: CancelRunSignal,
    ) -> None: ...


class RunManagementService:
    """Authorize owned Session Run operations and map the frozen R7 contract."""

    def __init__(
        self,
        access_resolver: RunAccessResolver,
        store: RunStore,
        workflow_control: RunWorkflowControl | None = None,
    ) -> None:
        self._access_resolver = access_resolver
        self._store = store
        self._workflow_control = workflow_control

    async def list_session_runs(
        self,
        principal: AuthenticatedPrincipal,
        *,
        session_id: str,
        limit: int,
        cursor: str | None,
        metadata: RequestMetadata,
    ) -> RunPage:
        access = await self._access_resolver.resolve_tenant_access(principal, metadata)
        if not access.allows("session", "read") or not access.allows("run", "list"):
            raise permission_denied()
        result = await self._store.list_session_runs(
            access.context,
            user_id=_resource_id(access.context.subject_id),
            session_id=_resource_id(session_id),
            limit=limit,
            cursor=cursor,
        )
        if result is None:
            raise resource_not_found()
        records, next_cursor = result
        return RunPage(
            items=[_run(record) for record in records],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    async def create_run(
        self,
        principal: AuthenticatedPrincipal,
        *,
        request: RunCreateRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> RunAccepted:
        access = await self._access_resolver.resolve_tenant_access(principal, metadata)
        if not access.allows("run", "create"):
            raise permission_denied()
        outcome = await self._store.create_run(
            access.context,
            user_id=_resource_id(access.context.subject_id),
            request=request,
            idempotency_key=idempotency_key,
            request_hash=canonical_request_hash("run.create", request),
            metadata=metadata,
        )
        if outcome.replay is not None:
            return RunAccepted.model_validate(outcome.replay.response_body)
        if outcome.value is None:
            raise RuntimeError("Run create outcome is missing its Run")
        return _accepted(outcome.value)

    async def get_run(
        self,
        principal: AuthenticatedPrincipal,
        *,
        run_id: str,
        metadata: RequestMetadata,
    ) -> Run:
        access = await self._access_resolver.resolve_tenant_access(principal, metadata)
        if not access.allows("run", "read"):
            raise permission_denied()
        record = await self._store.get_run(
            access.context,
            user_id=_resource_id(access.context.subject_id),
            run_id=_resource_id(run_id),
        )
        if record is None:
            raise resource_not_found()
        return _run(record)

    async def cancel_run(
        self,
        principal: AuthenticatedPrincipal,
        *,
        run_id: str,
        request: CancelRunRequest | None,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> Run:
        access = await self._access_resolver.resolve_tenant_access(principal, metadata)
        if not access.allows("run", "cancel"):
            raise permission_denied()
        parsed_run_id = _resource_id(run_id)
        body = request or CancelRunRequest(reason=None)
        outcome = await self._store.request_cancel(
            access.context,
            user_id=_resource_id(access.context.subject_id),
            run_id=parsed_run_id,
            request=body,
            idempotency_key=idempotency_key,
            request_hash=canonical_request_hash(
                "run.cancel", body, extra={"run_id": run_id}
            ),
            metadata=metadata,
        )
        if outcome is None:
            raise resource_not_found()
        record = await self._store.get_run(
            access.context,
            user_id=_resource_id(access.context.subject_id),
            run_id=parsed_run_id,
        )
        if record is None:
            raise resource_not_found()
        if record.status == "CANCELLING" and record.workflow_id is not None:
            if self._workflow_control is None:
                from packages.contracts.public import dependency_unavailable

                raise dependency_unavailable("Run Workflow control is not configured.")
            await self._workflow_control.signal_cancel(
                tenant_id=record.tenant_id,
                run_id=record.id,
                signal=CancelRunSignal(
                    signal_id=_cancel_signal_id(
                        record.tenant_id,
                        record.created_by,
                        record.id,
                        idempotency_key,
                    ),
                    requested_by=record.created_by,
                    requested_at=datetime.now(UTC),
                    reason=body.reason,
                ),
            )
        return _run(record)

    async def retry_run(
        self,
        principal: AuthenticatedPrincipal,
        *,
        run_id: str,
        request: RetryRunRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> RunAccepted:
        access = await self._access_resolver.resolve_tenant_access(principal, metadata)
        if not access.allows("run", "retry"):
            raise permission_denied()
        outcome = await self._store.retry_run(
            access.context,
            user_id=_resource_id(access.context.subject_id),
            run_id=_resource_id(run_id),
            request=request,
            idempotency_key=idempotency_key,
            request_hash=canonical_request_hash(
                "run.retry", request, extra={"run_id": run_id}
            ),
            metadata=metadata,
        )
        if outcome is None:
            raise resource_not_found()
        if outcome.replay is not None:
            return RunAccepted.model_validate(outcome.replay.response_body)
        if outcome.value is None:
            raise RuntimeError("Run retry outcome is missing its new Run")
        return _accepted(outcome.value)


def _accepted(record: RunRecord) -> RunAccepted:
    return RunAccepted(
        run_id=str(record.id),
        session_id=str(record.session_id),
        status=record.status,
        events_url=f"/api/v1/runs/{record.id}/events",
        stream_url=f"/api/v1/runs/{record.id}/events/stream",
    )


def _run(record: RunRecord) -> Run:
    error = None
    if record.error_code is not None:
        details = record.error_detail or {}
        error = Error(
            code=record.error_code,
            message=str(details.get("message", "Run execution failed.")),
            request_id=str(details.get("request_id", "unknown")),
            retryable=bool(details.get("retryable", False)),
            details=details or None,
        )
    return Run(
        id=str(record.id),
        session_id=str(record.session_id),
        snapshot_id=str(record.snapshot_id),
        deployment_id=str(record.deployment_id),
        retry_of_run_id=(
            str(record.retry_of_run_id) if record.retry_of_run_id is not None else None
        ),
        status=record.status,
        result_quality=record.result_quality,
        latest_sequence_no=record.latest_sequence_no,
        error=error,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )


def _resource_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise validation_error("Resource identifier is invalid.") from exc


def _cancel_signal_id(
    tenant_id: UUID, actor_id: UUID, run_id: UUID, idempotency_key: str
) -> str:
    value = f"{tenant_id}:{actor_id}:{run_id}:{idempotency_key}".encode()
    return f"cancel:{hashlib.sha256(value).hexdigest()}"

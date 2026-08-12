"""PostgreSQL persistence for SandboxInstance, Lease, and operation facts."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.policy import (
    AdmissionDenied,
    RuntimeBundleAdmissionFacts,
    admit_runtime_bundle,
)
from packages.application.reconciliation import SandboxReconciliationCandidate
from packages.application.sandbox.policy import FrozenSandboxPolicy
from packages.application.sandbox.service import (
    SandboxProvisionClaim,
    SandboxServiceAccess,
)
from packages.contracts.public import PlatformError, TenantContext
from packages.contracts.sandbox_api import (
    SandboxLeaseControlRequest,
    SandboxLeaseRequest,
    SandboxOperationAccepted,
    SandboxProvisionRequest,
    SandboxReleaseRequest,
)
from packages.domain.public import (
    SandboxInstanceRecord,
    SandboxLeaseRecord,
    SandboxStatus,
    ensure_sandbox_transition,
)
from packages.infrastructure.database.idempotency import (
    claim_idempotency,
    complete_idempotency,
)
from packages.infrastructure.database.models import (
    AgentRunModel,
    AuditLogModel,
    DeploymentModel,
    OperationRecordModel,
    RunAttemptModel,
    RuntimeBundleModel,
    SandboxInstanceModel,
    SandboxLeaseModel,
)
from packages.infrastructure.database.uow import TenantUnitOfWork
from packages.infrastructure.database.workspaces import (
    ensure_workspace_for_sandbox,
    transition_workspace_for_sandbox,
)


class SqlAlchemySandboxLifecycleStore:
    """Own all tenant-scoped Sandbox state transitions and fencing decisions."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_sandbox_reconciliation_candidates(
        self,
        context: TenantContext,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[SandboxReconciliationCandidate, ...]:
        if limit < 1:
            raise ValueError("Sandbox reconciliation limit must be positive")
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(
            self._session_factory, context, read_only=True
        ) as unit:
            rows = (
                await unit.session.execute(
                    select(SandboxInstanceModel, AgentRunModel.status)
                    .join(
                        AgentRunModel,
                        and_(
                            AgentRunModel.tenant_id == SandboxInstanceModel.tenant_id,
                            AgentRunModel.id == SandboxInstanceModel.run_id,
                        ),
                    )
                    .where(
                        SandboxInstanceModel.tenant_id == tenant_id,
                        SandboxInstanceModel.status != "TERMINATED",
                        or_(
                            and_(
                                SandboxInstanceModel.scope == "run",
                                AgentRunModel.status.in_(
                                    ("SUCCEEDED", "FAILED", "CANCELLED", "TIMEOUT")
                                ),
                            ),
                            and_(
                                SandboxInstanceModel.status.in_(("READY", "IN_USE")),
                                SandboxInstanceModel.lease_expires_at.is_not(None),
                                SandboxInstanceModel.lease_expires_at <= now,
                            ),
                        ),
                    )
                    .order_by(
                        SandboxInstanceModel.updated_at,
                        SandboxInstanceModel.id,
                    )
                    .limit(limit)
                )
            ).all()
        terminal_statuses = {"SUCCEEDED", "FAILED", "CANCELLED", "TIMEOUT"}
        return tuple(
            SandboxReconciliationCandidate(
                sandbox_id=row.id,
                reason=(
                    "TERMINAL_RUN"
                    if row.scope == "run" and run_status in terminal_statuses
                    else (
                        "EXPIRED_RUN_LEASE"
                        if row.scope == "run"
                        else "EXPIRED_SESSION_LEASE"
                    )
                ),
                action="DESTROY" if row.scope == "run" else "MANUAL",
            )
            for row, run_status in rows
        )

    async def begin_provision(
        self,
        access: SandboxServiceAccess,
        *,
        request: SandboxProvisionRequest,
        idempotency_key: str,
        request_hash: str,
        policy: FrozenSandboxPolicy,
        now: datetime,
    ) -> SandboxProvisionClaim:
        tenant_id = UUID(access.context.tenant_id)
        actor_id = UUID(access.context.subject_id)
        run_id = _uuid(request.run_id, "run_id")
        user_id = _uuid(request.user_id, "user_id")
        session_id = _uuid(request.session_id, "session_id")
        async with TenantUnitOfWork(self._session_factory, access.context) as unit:
            session = unit.session
            idempotency_id, replay = await claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type="sandbox.provision",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                accepted = SandboxOperationAccepted.model_validate(replay.response_body)
                return SandboxProvisionClaim(
                    accepted=accepted,
                    instance=None,
                    idempotency_record_id=None,
                    replayed=True,
                )
            run = await session.scalar(
                select(AgentRunModel)
                .where(
                    AgentRunModel.tenant_id == tenant_id,
                    AgentRunModel.id == run_id,
                    AgentRunModel.session_id == session_id,
                    AgentRunModel.created_by == user_id,
                )
                .with_for_update()
            )
            if run is None:
                raise _sandbox_error(
                    404,
                    "RESOURCE_NOT_FOUND",
                    "The Run identity for Sandbox provisioning is unavailable.",
                )
            if (
                run.status not in {"PREPARING", "RUNNING"}
                or run.current_attempt != request.execution_attempt
            ):
                raise _sandbox_error(
                    409,
                    "SANDBOX_FENCING_REJECTED",
                    "The Run attempt is not current for Sandbox provisioning.",
                )
            attempt = await session.scalar(
                select(RunAttemptModel).where(
                    RunAttemptModel.tenant_id == tenant_id,
                    RunAttemptModel.run_id == run_id,
                    RunAttemptModel.attempt_no == request.execution_attempt,
                )
            )
            if attempt is None or attempt.status not in {
                "ALLOCATED",
                "STARTING",
                "RUNNING",
            }:
                raise _sandbox_error(
                    409,
                    "SANDBOX_FENCING_REJECTED",
                    "The Run attempt cannot provision a Sandbox.",
                )
            deployment = await session.scalar(
                select(DeploymentModel).where(
                    DeploymentModel.tenant_id == tenant_id,
                    DeploymentModel.id == run.deployment_id,
                    DeploymentModel.snapshot_id == run.snapshot_id,
                )
            )
            if deployment is None:
                raise _sandbox_error(
                    404,
                    "RESOURCE_NOT_FOUND",
                    "The Run Deployment is unavailable.",
                )
            bundle = await session.scalar(
                select(RuntimeBundleModel).where(
                    RuntimeBundleModel.tenant_id == tenant_id,
                    RuntimeBundleModel.id == deployment.bundle_id,
                    RuntimeBundleModel.snapshot_id == run.snapshot_id,
                    RuntimeBundleModel.content_hash == request.bundle_hash,
                    RuntimeBundleModel.scan_status == "PASSED",
                )
            )
            if (
                bundle is None
                or request.bundle_ref != _logical_bundle_ref(bundle)
                or _manifest_policy_hash(bundle.manifest_json) != policy.policy_hash
            ):
                raise _sandbox_error(
                    403,
                    "SANDBOX_POLICY_DENIED",
                    "The Run Bundle and Sandbox policy do not match immutable facts.",
                )
            try:
                admit_runtime_bundle(
                    RuntimeBundleAdmissionFacts(
                        compiler_name=bundle.compiler_name,
                        compiler_version=bundle.compiler_version,
                        scan_status=bundle.scan_status,
                        manifest=bundle.manifest_json,
                    )
                )
            except AdmissionDenied as error:
                raise _sandbox_error(403, error.code, str(error)) from error
            await ensure_workspace_for_sandbox(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
                run_id=run_id,
                workspace_uri=request.workspace_uri,
                policy=policy,
                now=now,
            )
            sandbox_id = uuid4()
            operation_id = uuid4()
            accepted = SandboxOperationAccepted(
                sandbox_id=str(sandbox_id),
                operation_id=str(operation_id),
                status_url=f"/api/v1/operations/{operation_id}",
            )
            operation = OperationRecordModel(
                id=operation_id,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type="sandbox.provision",
                status="ACCEPTED",
                resource_type="sandbox",
                resource_id=sandbox_id,
                created_at=now,
                updated_at=now,
            )
            instance = SandboxInstanceModel(
                id=sandbox_id,
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
                run_id=run_id,
                execution_attempt=request.execution_attempt,
                scope=request.scope,
                image_digest=policy.load().image_digest,
                policy_ref=request.policy_ref,
                policy_hash=policy.policy_hash,
                policy_schema_version=policy.schema_version,
                policy_json=cast(dict[str, object], json.loads(policy.canonical_json)),
                bundle_ref=request.bundle_ref,
                bundle_hash=request.bundle_hash,
                workspace_uri=request.workspace_uri,
                runtime_target_id=deployment.runtime_target_id,
                status="REQUESTED",
                provision_operation_id=operation_id,
                created_at=now,
                updated_at=now,
            )
            session.add_all((operation, instance))
            await _audit(
                session,
                access,
                action="sandbox.provision.requested",
                sandbox_id=sandbox_id,
                result="SUCCESS",
                metadata={
                    "run_id": str(run_id),
                    "execution_attempt": request.execution_attempt,
                    "operation_id": str(operation_id),
                    "policy_hash": policy.policy_hash,
                    "bundle_hash": request.bundle_hash,
                },
                now=now,
            )
            await session.flush()
            return SandboxProvisionClaim(
                accepted=accepted,
                instance=_instance_record(instance),
                idempotency_record_id=idempotency_id,
                replayed=False,
            )

    async def mark_provisioning(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: UUID,
        now: datetime,
    ) -> SandboxInstanceRecord:
        async with TenantUnitOfWork(self._session_factory, access.context) as unit:
            instance = await _locked_instance(unit.session, access, sandbox_id)
            if instance.status == "REQUESTED":
                ensure_sandbox_transition("REQUESTED", "PROVISIONING")
                instance.status = "PROVISIONING"
                instance.updated_at = now
            operation = await _locked_operation(
                unit.session, access, instance.provision_operation_id
            )
            if operation.status == "ACCEPTED":
                operation.status = "RUNNING"
                operation.updated_at = now
            await unit.session.flush()
            return _instance_record(instance)

    async def finish_provision(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: UUID,
        idempotency_record_id: UUID,
        accepted: SandboxOperationAccepted,
        status: SandboxStatus,
        provider_ref: str | None,
        failure_code: str | None,
        operation_status: Literal["RUNNING", "SUCCEEDED", "FAILED"],
        operation_error: dict[str, object] | None,
        now: datetime,
    ) -> SandboxInstanceRecord:
        async with TenantUnitOfWork(self._session_factory, access.context) as unit:
            session = unit.session
            instance = await _locked_instance(session, access, sandbox_id)
            current = cast(SandboxStatus, instance.status)
            ensure_sandbox_transition(current, status)
            instance.status = status
            if provider_ref is not None:
                instance.provider_ref = provider_ref
            instance.failure_code = failure_code
            instance.updated_at = now
            if status == "FAILED":
                await transition_workspace_for_sandbox(
                    session,
                    tenant_id=UUID(access.context.tenant_id),
                    workspace_uri=instance.workspace_uri,
                    status="SEALED",
                    now=now,
                )
            operation = await _locked_operation(
                session, access, instance.provision_operation_id
            )
            operation.status = operation_status
            operation.error_json = operation_error
            operation.result_json = (
                {"sandbox_id": str(instance.id), "status": status}
                if operation_error is None
                else None
            )
            operation.updated_at = now
            operation.finished_at = (
                now if operation_status in {"SUCCEEDED", "FAILED"} else None
            )
            await complete_idempotency(
                session,
                idempotency_record_id,
                response_status=202,
                response_body=accepted.model_dump(mode="json"),
                response_etag=None,
                response_ref=str(instance.id),
            )
            await _audit(
                session,
                access,
                action="sandbox.provision.completed",
                sandbox_id=instance.id,
                result="FAILED" if operation_status == "FAILED" else "SUCCESS",
                metadata={
                    "status": status,
                    "operation_status": operation_status,
                    "failure_code": failure_code,
                },
                now=now,
            )
            await session.flush()
            return _instance_record(instance)

    async def get_instance(
        self, access: SandboxServiceAccess, *, sandbox_id: UUID
    ) -> SandboxInstanceRecord | None:
        tenant_id = UUID(access.context.tenant_id)
        async with TenantUnitOfWork(
            self._session_factory, access.context, read_only=True
        ) as unit:
            instance = await unit.session.scalar(
                select(SandboxInstanceModel).where(
                    SandboxInstanceModel.tenant_id == tenant_id,
                    SandboxInstanceModel.id == sandbox_id,
                )
            )
            return _instance_record(instance) if instance is not None else None

    async def get_active_lease(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: UUID,
        now: datetime,
    ) -> SandboxLeaseRecord | None:
        tenant_id = UUID(access.context.tenant_id)
        async with TenantUnitOfWork(
            self._session_factory, access.context, read_only=True
        ) as unit:
            lease = await unit.session.scalar(
                select(SandboxLeaseModel).where(
                    SandboxLeaseModel.tenant_id == tenant_id,
                    SandboxLeaseModel.sandbox_id == sandbox_id,
                    SandboxLeaseModel.released_at.is_(None),
                    SandboxLeaseModel.expires_at > now,
                )
            )
            return _lease_record(lease) if lease is not None else None

    async def acquire_lease(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: UUID,
        request: SandboxLeaseRequest,
        now: datetime,
    ) -> SandboxLeaseRecord | None:
        tenant_id = UUID(access.context.tenant_id)
        run_id = _uuid(request.run_id, "run_id")
        token_hash = _sha256(request.execution_fencing_token.get_secret_value())
        async with TenantUnitOfWork(self._session_factory, access.context) as unit:
            session = unit.session
            instance = await session.scalar(
                select(SandboxInstanceModel)
                .where(
                    SandboxInstanceModel.tenant_id == tenant_id,
                    SandboxInstanceModel.id == sandbox_id,
                )
                .with_for_update()
            )
            if instance is None:
                return None
            if (
                instance.scope != "run"
                or instance.run_id != run_id
                or instance.execution_attempt != request.execution_attempt
            ):
                raise _sandbox_error(
                    409,
                    "SANDBOX_LEASE_CONFLICT",
                    "Sandbox is bound to another Run attempt.",
                )
            attempt = await session.scalar(
                select(RunAttemptModel).where(
                    RunAttemptModel.tenant_id == tenant_id,
                    RunAttemptModel.run_id == run_id,
                    RunAttemptModel.attempt_no == request.execution_attempt,
                )
            )
            if (
                attempt is None
                or attempt.status not in {"ALLOCATED", "STARTING", "RUNNING"}
                or not hmac.compare_digest(attempt.fencing_token_hash, token_hash)
            ):
                raise _sandbox_error(
                    409,
                    "SANDBOX_FENCING_REJECTED",
                    "The execution fencing token is stale or invalid.",
                )
            active = await session.scalar(
                select(SandboxLeaseModel)
                .where(
                    SandboxLeaseModel.tenant_id == tenant_id,
                    SandboxLeaseModel.sandbox_id == sandbox_id,
                    SandboxLeaseModel.released_at.is_(None),
                )
                .with_for_update()
            )
            if active is not None and active.expires_at > now:
                if (
                    active.holder_run_id == run_id
                    and active.execution_attempt == request.execution_attempt
                    and hmac.compare_digest(active.fencing_token_hash, token_hash)
                ):
                    return _lease_record(active)
                raise _sandbox_error(
                    409,
                    "SANDBOX_LEASE_CONFLICT",
                    "Sandbox already has an active Lease.",
                )
            if active is not None:
                active.released_at = now
            if instance.status not in {"READY", "IN_USE"}:
                raise _sandbox_error(
                    409,
                    "SANDBOX_NOT_READY",
                    "Sandbox is not ready for a Lease.",
                )
            expires_at = now + timedelta(seconds=request.ttl_seconds)
            lease = SandboxLeaseModel(
                id=uuid4(),
                tenant_id=tenant_id,
                sandbox_id=sandbox_id,
                holder_run_id=run_id,
                execution_attempt=request.execution_attempt,
                fencing_token_hash=token_hash,
                acquired_at=now,
                expires_at=expires_at,
            )
            session.add(lease)
            if instance.status == "READY":
                ensure_sandbox_transition("READY", "IN_USE")
                instance.status = "IN_USE"
            instance.lease_expires_at = expires_at
            instance.updated_at = now
            await _audit(
                session,
                access,
                action="sandbox.lease.acquired",
                sandbox_id=sandbox_id,
                result="SUCCESS",
                metadata={
                    "lease_id": str(lease.id),
                    "run_id": str(run_id),
                    "execution_attempt": request.execution_attempt,
                    "expires_at": expires_at.isoformat(),
                },
                now=now,
            )
            await session.flush()
            return _lease_record(lease)

    async def release_lease(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: UUID,
        request: SandboxReleaseRequest,
        now: datetime,
    ) -> tuple[SandboxInstanceRecord, bool] | None:
        async with TenantUnitOfWork(self._session_factory, access.context) as unit:
            session = unit.session
            instance = await _optional_locked_instance(session, access, sandbox_id)
            if instance is None:
                return None
            lease = await _require_fenced_lease(
                session,
                access,
                instance=instance,
                request=request,
                now=now,
                allow_released=True,
                require_active_attempt=False,
            )
            changed = False
            if lease.released_at is None:
                lease.released_at = now
                changed = True
            instance.lease_expires_at = None
            if instance.scope == "run" and instance.status in {"READY", "IN_USE"}:
                ensure_sandbox_transition(
                    cast(SandboxStatus, instance.status), "TERMINATING"
                )
                instance.status = "TERMINATING"
                changed = True
                await transition_workspace_for_sandbox(
                    session,
                    tenant_id=UUID(access.context.tenant_id),
                    workspace_uri=instance.workspace_uri,
                    status="SEALED",
                    now=now,
                )
            elif instance.scope == "session" and instance.status == "IN_USE":
                ensure_sandbox_transition("IN_USE", "READY")
                instance.status = "READY"
                changed = True
            instance.updated_at = now
            await _audit(
                session,
                access,
                action="sandbox.lease.released",
                sandbox_id=sandbox_id,
                result="SUCCESS",
                metadata={"changed": changed, "status": instance.status},
                now=now,
            )
            await session.flush()
            return _instance_record(instance), changed

    async def authorize_lease_control(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: UUID,
        request: SandboxLeaseControlRequest,
        now: datetime,
    ) -> SandboxInstanceRecord | None:
        async with TenantUnitOfWork(self._session_factory, access.context) as unit:
            instance = await _optional_locked_instance(unit.session, access, sandbox_id)
            if instance is None:
                return None
            await _require_fenced_lease(
                unit.session,
                access,
                instance=instance,
                request=request,
                now=now,
                allow_released=False,
                require_active_attempt=True,
            )
            return _instance_record(instance)

    async def begin_termination(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: UUID,
        now: datetime,
    ) -> tuple[SandboxInstanceRecord, bool] | None:
        async with TenantUnitOfWork(self._session_factory, access.context) as unit:
            session = unit.session
            instance = await _optional_locked_instance(session, access, sandbox_id)
            if instance is None:
                return None
            if instance.status == "TERMINATED":
                return _instance_record(instance), False
            changed = instance.status != "TERMINATING"
            if changed:
                ensure_sandbox_transition(
                    cast(SandboxStatus, instance.status), "TERMINATING"
                )
                instance.status = "TERMINATING"
            lease = await session.scalar(
                select(SandboxLeaseModel)
                .where(
                    SandboxLeaseModel.tenant_id == UUID(access.context.tenant_id),
                    SandboxLeaseModel.sandbox_id == sandbox_id,
                    SandboxLeaseModel.released_at.is_(None),
                )
                .with_for_update()
            )
            if lease is not None:
                lease.released_at = now
                changed = True
            instance.lease_expires_at = None
            instance.updated_at = now
            if instance.scope == "run":
                await transition_workspace_for_sandbox(
                    session,
                    tenant_id=UUID(access.context.tenant_id),
                    workspace_uri=instance.workspace_uri,
                    status="SEALED",
                    now=now,
                )
            await _audit(
                session,
                access,
                action="sandbox.termination.requested",
                sandbox_id=sandbox_id,
                result="SUCCESS",
                metadata={"changed": changed},
                now=now,
            )
            await session.flush()
            return _instance_record(instance), changed

    async def finish_termination(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: UUID,
        status: Literal["TERMINATED", "QUARANTINED"],
        provider_ref: str | None,
        failure_code: str | None,
        now: datetime,
    ) -> SandboxInstanceRecord:
        async with TenantUnitOfWork(self._session_factory, access.context) as unit:
            session = unit.session
            instance = await _locked_instance(session, access, sandbox_id)
            current = cast(SandboxStatus, instance.status)
            ensure_sandbox_transition(current, status)
            instance.status = status
            if provider_ref is not None:
                instance.provider_ref = provider_ref
            instance.failure_code = failure_code
            instance.updated_at = now
            instance.terminated_at = now if status == "TERMINATED" else None
            if instance.scope == "run":
                await transition_workspace_for_sandbox(
                    session,
                    tenant_id=UUID(access.context.tenant_id),
                    workspace_uri=instance.workspace_uri,
                    status="QUARANTINED" if status == "QUARANTINED" else "SEALED",
                    now=now,
                )
            await _audit(
                session,
                access,
                action="sandbox.destroy.completed",
                sandbox_id=sandbox_id,
                result="SUCCESS" if status == "TERMINATED" else "FAILED",
                metadata={"status": status, "failure_code": failure_code},
                now=now,
            )
            await session.flush()
            return _instance_record(instance)


async def _require_fenced_lease(
    session: AsyncSession,
    access: SandboxServiceAccess,
    *,
    instance: SandboxInstanceModel,
    request: SandboxLeaseControlRequest,
    now: datetime,
    allow_released: bool,
    require_active_attempt: bool,
) -> SandboxLeaseModel:
    tenant_id = UUID(access.context.tenant_id)
    run_id = _uuid(request.run_id, "run_id")
    token_hash = _sha256(request.execution_fencing_token.get_secret_value())
    if (
        instance.run_id != run_id
        or instance.execution_attempt != request.execution_attempt
    ):
        raise _sandbox_error(
            409,
            "SANDBOX_LEASE_CONFLICT",
            "Sandbox is controlled by another Run attempt.",
        )
    lease = await session.scalar(
        select(SandboxLeaseModel)
        .where(
            SandboxLeaseModel.tenant_id == tenant_id,
            SandboxLeaseModel.sandbox_id == instance.id,
            SandboxLeaseModel.released_at.is_(None),
        )
        .with_for_update()
    )
    if lease is None and allow_released:
        lease = await session.scalar(
            select(SandboxLeaseModel)
            .where(
                SandboxLeaseModel.tenant_id == tenant_id,
                SandboxLeaseModel.sandbox_id == instance.id,
                SandboxLeaseModel.holder_run_id == run_id,
                SandboxLeaseModel.execution_attempt == request.execution_attempt,
            )
            .order_by(SandboxLeaseModel.acquired_at.desc())
            .limit(1)
            .with_for_update()
        )
    if lease is None or (
        not allow_released
        and (lease.released_at is not None or lease.expires_at <= now)
    ):
        raise _sandbox_error(
            409,
            "SANDBOX_LEASE_CONFLICT",
            "Sandbox does not have an active Lease.",
        )
    attempt = await session.scalar(
        select(RunAttemptModel).where(
            RunAttemptModel.tenant_id == tenant_id,
            RunAttemptModel.run_id == run_id,
            RunAttemptModel.attempt_no == request.execution_attempt,
        )
    )
    if (
        lease.holder_run_id != run_id
        or lease.execution_attempt != request.execution_attempt
        or not hmac.compare_digest(lease.fencing_token_hash, token_hash)
        or attempt is None
        or (
            require_active_attempt
            and attempt.status not in {"ALLOCATED", "STARTING", "RUNNING"}
        )
        or not hmac.compare_digest(attempt.fencing_token_hash, token_hash)
    ):
        raise _sandbox_error(
            409,
            "SANDBOX_FENCING_REJECTED",
            "The execution fencing token is stale or invalid.",
        )
    return lease


async def _locked_instance(
    session: AsyncSession, access: SandboxServiceAccess, sandbox_id: UUID
) -> SandboxInstanceModel:
    instance = await _optional_locked_instance(session, access, sandbox_id)
    if instance is None:
        raise RuntimeError("Sandbox instance disappeared during lifecycle transition")
    return instance


async def _optional_locked_instance(
    session: AsyncSession, access: SandboxServiceAccess, sandbox_id: UUID
) -> SandboxInstanceModel | None:
    return await session.scalar(
        select(SandboxInstanceModel)
        .where(
            SandboxInstanceModel.tenant_id == UUID(access.context.tenant_id),
            SandboxInstanceModel.id == sandbox_id,
        )
        .with_for_update()
    )


async def _locked_operation(
    session: AsyncSession, access: SandboxServiceAccess, operation_id: UUID
) -> OperationRecordModel:
    operation = await session.scalar(
        select(OperationRecordModel)
        .where(
            OperationRecordModel.tenant_id == UUID(access.context.tenant_id),
            OperationRecordModel.id == operation_id,
            OperationRecordModel.operation_type == "sandbox.provision",
        )
        .with_for_update()
    )
    if operation is None:
        raise RuntimeError("Sandbox operation is unavailable")
    return operation


async def _audit(
    session: AsyncSession,
    access: SandboxServiceAccess,
    *,
    action: str,
    sandbox_id: UUID,
    result: Literal["SUCCESS", "FAILED", "DENIED"],
    metadata: dict[str, object],
    now: datetime,
) -> None:
    session.add(
        AuditLogModel(
            tenant_id=UUID(access.context.tenant_id),
            actor_type="service",
            actor_id=UUID(access.context.subject_id),
            action=action,
            resource_type="sandbox",
            resource_id=sandbox_id,
            result=result,
            reason_codes=(
                [str(metadata["failure_code"])]
                if metadata.get("failure_code") is not None
                else []
            ),
            request_id=access.context.request_id,
            trace_id=access.context.trace_id,
            metadata_schema_version=1,
            metadata_json=metadata,
            created_at=now,
        )
    )


def _instance_record(model: SandboxInstanceModel) -> SandboxInstanceRecord:
    return SandboxInstanceRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        user_id=model.user_id,
        session_id=model.session_id,
        run_id=model.run_id,
        execution_attempt=model.execution_attempt,
        scope=cast(Literal["run", "session"], model.scope),
        image_digest=model.image_digest,
        policy_ref=model.policy_ref,
        policy_hash=model.policy_hash,
        policy_schema_version=model.policy_schema_version,
        policy_json=model.policy_json,
        bundle_ref=model.bundle_ref,
        bundle_hash=model.bundle_hash,
        workspace_uri=model.workspace_uri,
        runtime_target_id=model.runtime_target_id,
        status=cast(SandboxStatus, model.status),
        provider_ref=model.provider_ref,
        lease_expires_at=model.lease_expires_at,
        provision_operation_id=model.provision_operation_id,
        created_at=model.created_at,
        updated_at=model.updated_at,
        terminated_at=model.terminated_at,
        failure_code=model.failure_code,
    )


def _lease_record(model: SandboxLeaseModel) -> SandboxLeaseRecord:
    return SandboxLeaseRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        sandbox_id=model.sandbox_id,
        holder_run_id=model.holder_run_id,
        execution_attempt=model.execution_attempt,
        fencing_token_hash=model.fencing_token_hash,
        acquired_at=model.acquired_at,
        expires_at=model.expires_at,
        released_at=model.released_at,
    )


def _manifest_policy_hash(manifest: dict[str, object]) -> str | None:
    security = manifest.get("security")
    if not isinstance(security, Mapping):
        return None
    value = cast(Mapping[str, object], security).get("sandbox_policy_hash")
    return value if isinstance(value, str) else None


def _logical_bundle_ref(bundle: RuntimeBundleModel) -> str:
    return (
        f"bundle://tenant/{bundle.tenant_id}/snapshot/{bundle.snapshot_id}/"
        f"runtime/{bundle.runtime_type}/{bundle.content_hash}"
    )


def _sha256(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _uuid(value: str, field_name: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as error:
        raise _sandbox_error(
            400, "VALIDATION_ERROR", f"{field_name} must be a UUID."
        ) from error


def _sandbox_error(status_code: int, code: str, message: str) -> PlatformError:
    return PlatformError(status_code=status_code, code=code, message=message)

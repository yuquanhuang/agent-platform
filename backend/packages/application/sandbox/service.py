"""Service-authenticated Sandbox lifecycle orchestration over explicit ports."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, TypeVar
from uuid import UUID

from packages.application.sandbox.policy import (
    FrozenSandboxPolicy,
    compile_sandbox_policy,
)
from packages.application.sandbox.provider import (
    ProviderProcessObservation,
    ProviderProvisionSpec,
    ProviderSandboxObservation,
    SandboxProvider,
    SandboxProviderError,
)
from packages.contracts.public import (
    PlatformError,
    SubjectType,
    TenantContext,
    permission_denied,
    resource_not_found,
    unauthenticated,
)
from packages.contracts.sandbox_api import (
    SandboxActionResponse,
    SandboxDetailResponse,
    SandboxInspectResponse,
    SandboxLeaseControlRequest,
    SandboxLeaseRequest,
    SandboxLeaseResponse,
    SandboxMainProcess,
    SandboxOperationAccepted,
    SandboxProcessActionResponse,
    SandboxProcessControlRequest,
    SandboxProcessRequest,
    SandboxProcessResponse,
    SandboxProvisionRequest,
    SandboxReleaseRequest,
    SandboxResourceUsage,
)
from packages.domain.public import (
    SandboxInstanceRecord,
    SandboxLeaseRecord,
    SandboxStatus,
    WorkspaceUri,
)

SANDBOX_MANAGE_PERMISSION = "internal:sandbox_manage"
ProviderResult = TypeVar("ProviderResult")


@dataclass(frozen=True, slots=True)
class SandboxServiceAccess:
    """Trusted workload identity resolved by mTLS or Workload Identity."""

    context: TenantContext
    permissions: frozenset[str]

    def authorize(self) -> None:
        if self.context.subject_type is not SubjectType.SERVICE:
            raise unauthenticated("A service workload identity is required.")
        if SANDBOX_MANAGE_PERMISSION not in self.permissions:
            raise permission_denied(
                "The service identity cannot manage Sandbox instances."
            )


@dataclass(frozen=True, slots=True)
class SandboxProvisionClaim:
    accepted: SandboxOperationAccepted
    instance: SandboxInstanceRecord | None
    idempotency_record_id: UUID | None
    replayed: bool


class SandboxPolicyResolver(Protocol):
    """Resolve only an immutable policy snapshot matching the supplied hash."""

    async def resolve(
        self,
        context: TenantContext,
        *,
        policy_ref: str,
        policy_hash: str,
    ) -> FrozenSandboxPolicy | None: ...


class SandboxProvisionTokenVerifier(Protocol):
    """Consume a token idempotently for one key and non-secret request hash."""

    async def verify(
        self,
        access: SandboxServiceAccess,
        *,
        request: SandboxProvisionRequest,
        idempotency_key: str,
        request_hash: str,
    ) -> None: ...


class SandboxLifecycleStore(Protocol):
    async def begin_provision(
        self,
        access: SandboxServiceAccess,
        *,
        request: SandboxProvisionRequest,
        idempotency_key: str,
        request_hash: str,
        policy: FrozenSandboxPolicy,
        now: datetime,
    ) -> SandboxProvisionClaim: ...

    async def mark_provisioning(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: UUID,
        now: datetime,
    ) -> SandboxInstanceRecord: ...

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
    ) -> SandboxInstanceRecord: ...

    async def get_instance(
        self, access: SandboxServiceAccess, *, sandbox_id: UUID
    ) -> SandboxInstanceRecord | None: ...

    async def get_active_lease(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: UUID,
        now: datetime,
    ) -> SandboxLeaseRecord | None: ...

    async def acquire_lease(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: UUID,
        request: SandboxLeaseRequest,
        now: datetime,
    ) -> SandboxLeaseRecord | None: ...

    async def authorize_lease_control(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: UUID,
        request: SandboxLeaseControlRequest,
        now: datetime,
    ) -> SandboxInstanceRecord | None: ...

    async def release_lease(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: UUID,
        request: SandboxReleaseRequest,
        now: datetime,
    ) -> tuple[SandboxInstanceRecord, bool] | None: ...

    async def begin_termination(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: UUID,
        now: datetime,
    ) -> tuple[SandboxInstanceRecord, bool] | None: ...

    async def finish_termination(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: UUID,
        status: Literal["TERMINATED", "QUARANTINED"],
        provider_ref: str | None,
        failure_code: str | None,
        now: datetime,
    ) -> SandboxInstanceRecord: ...


class SandboxInternalService(Protocol):
    async def provision(
        self,
        access: SandboxServiceAccess,
        *,
        request: SandboxProvisionRequest,
        idempotency_key: str,
    ) -> SandboxOperationAccepted: ...

    async def get(
        self, access: SandboxServiceAccess, *, sandbox_id: str
    ) -> SandboxDetailResponse: ...

    async def inspect(
        self, access: SandboxServiceAccess, *, sandbox_id: str
    ) -> SandboxInspectResponse: ...

    async def acquire_lease(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: str,
        request: SandboxLeaseRequest,
    ) -> SandboxLeaseResponse: ...

    async def start_process(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: str,
        request: SandboxProcessRequest,
    ) -> SandboxProcessResponse: ...

    async def cancel_process(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: str,
        process_id: str,
        request: SandboxProcessControlRequest,
    ) -> SandboxProcessActionResponse: ...

    async def terminate(
        self, access: SandboxServiceAccess, *, sandbox_id: str
    ) -> SandboxActionResponse: ...

    async def release(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: str,
        request: SandboxReleaseRequest,
    ) -> SandboxActionResponse: ...

    async def destroy(
        self, access: SandboxServiceAccess, *, sandbox_id: str
    ) -> SandboxActionResponse: ...


class SandboxLifecycleService:
    """Persist fencing authority around bounded, idempotent Provider calls."""

    def __init__(
        self,
        store: SandboxLifecycleStore,
        provider: SandboxProvider,
        policy_resolver: SandboxPolicyResolver,
        token_verifier: SandboxProvisionTokenVerifier,
        *,
        provider_timeout_seconds: float = 60.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if provider_timeout_seconds <= 0:
            raise ValueError("provider_timeout_seconds must be positive")
        self._store = store
        self._provider = provider
        self._policy_resolver = policy_resolver
        self._token_verifier = token_verifier
        self._provider_timeout_seconds = provider_timeout_seconds
        self._now = now

    async def provision(
        self,
        access: SandboxServiceAccess,
        *,
        request: SandboxProvisionRequest,
        idempotency_key: str,
    ) -> SandboxOperationAccepted:
        access.authorize()
        if request.scope != "run":
            raise _sandbox_error(
                403,
                "SANDBOX_POLICY_DENIED",
                "Session Sandbox provisioning is not enabled for the MVP.",
            )
        _require_request_tenant(access, request.tenant_id)
        _require_run_workspace_binding(request)
        request_hash = _provision_request_hash(request)
        await self._token_verifier.verify(
            access,
            request=request,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        policy = await self._policy_resolver.resolve(
            access.context,
            policy_ref=request.policy_ref,
            policy_hash=request.policy_hash,
        )
        if policy is None or policy.policy_hash != request.policy_hash:
            raise _sandbox_error(
                403,
                "SANDBOX_POLICY_DENIED",
                "The immutable Sandbox policy is unavailable or mismatched.",
            )
        now = self._now()
        claim = await self._store.begin_provision(
            access,
            request=request,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            policy=policy,
            now=now,
        )
        if claim.replayed:
            return claim.accepted
        if claim.instance is None or claim.idempotency_record_id is None:
            raise RuntimeError("Sandbox provision claim is incomplete")
        instance = await self._store.mark_provisioning(
            access,
            sandbox_id=claim.instance.id,
            now=self._now(),
        )
        try:
            observation = await self._provider_call(
                self._provider.provision(
                    ProviderProvisionSpec(
                        sandbox_id=str(instance.id),
                        tenant_id=str(instance.tenant_id),
                        run_id=str(instance.run_id),
                        image_digest=instance.image_digest,
                        bundle_ref=instance.bundle_ref,
                        bundle_hash=instance.bundle_hash,
                        workspace_uri=instance.workspace_uri,
                        policy=policy,
                    )
                )
            )
            status, operation_status, failure_code = _provision_outcome(observation)
            await self._store.finish_provision(
                access,
                sandbox_id=instance.id,
                idempotency_record_id=claim.idempotency_record_id,
                accepted=claim.accepted,
                status=status,
                provider_ref=observation.provider_ref,
                failure_code=failure_code,
                operation_status=operation_status,
                operation_error=(
                    None
                    if failure_code is None
                    else _safe_operation_error(failure_code)
                ),
                now=self._now(),
            )
        except (SandboxProviderError, TimeoutError) as error:
            failure_code = (
                error.code
                if isinstance(error, SandboxProviderError)
                else "SANDBOX_PROVIDER_TIMEOUT"
            )
            await self._store.finish_provision(
                access,
                sandbox_id=instance.id,
                idempotency_record_id=claim.idempotency_record_id,
                accepted=claim.accepted,
                status="FAILED",
                provider_ref=None,
                failure_code=failure_code,
                operation_status="FAILED",
                operation_error=_safe_operation_error("SANDBOX_PROVISION_FAILED"),
                now=self._now(),
            )
        return claim.accepted

    async def get(
        self, access: SandboxServiceAccess, *, sandbox_id: str
    ) -> SandboxDetailResponse:
        record = await self._required_instance(access, sandbox_id)
        return _detail_response(record)

    async def inspect(
        self, access: SandboxServiceAccess, *, sandbox_id: str
    ) -> SandboxInspectResponse:
        record = await self._required_instance(access, sandbox_id)
        lease = await self._store.get_active_lease(
            access,
            sandbox_id=record.id,
            now=self._now(),
        )
        observation: ProviderSandboxObservation | None = None
        if record.provider_ref is not None and record.status not in {
            "FAILED",
            "TERMINATED",
        }:
            try:
                observation = await self._provider_call(
                    self._provider.inspect(record.provider_ref)
                )
            except (SandboxProviderError, TimeoutError) as error:
                raise _provider_platform_error(
                    error,
                    code="SANDBOX_PROVISION_FAILED",
                    message="The Sandbox Provider could not be inspected.",
                ) from error
        return _inspect_response(record, lease, observation, now=self._now())

    async def acquire_lease(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: str,
        request: SandboxLeaseRequest,
    ) -> SandboxLeaseResponse:
        access.authorize()
        record = await self._store.acquire_lease(
            access,
            sandbox_id=_uuid(sandbox_id, "sandbox_id"),
            request=request,
            now=self._now(),
        )
        if record is None:
            raise resource_not_found("Sandbox was not found.")
        return _lease_response(record)

    async def start_process(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: str,
        request: SandboxProcessRequest,
    ) -> SandboxProcessResponse:
        record, policy = await self._process_ready_instance(access, sandbox_id, request)
        if request.argv[0] not in policy.load().process.allowed_executables:
            raise _sandbox_error(
                403,
                "SANDBOX_POLICY_DENIED",
                "The executable is not allowed by the Sandbox policy.",
            )
        if request.timeout_seconds > policy.load().timeout_seconds:
            raise _sandbox_error(
                403,
                "SANDBOX_POLICY_DENIED",
                "The process timeout exceeds the Sandbox policy.",
            )
        _require_workspace_child(record.workspace_uri, request.working_directory)
        assert record.provider_ref is not None
        try:
            observation = await self._provider_call(
                self._provider.start_process(
                    record.provider_ref,
                    process_id=request.process_id,
                    argv=tuple(request.argv),
                    working_directory=request.working_directory,
                    environment_refs=tuple(request.environment_refs),
                    timeout_seconds=request.timeout_seconds,
                )
            )
        except (SandboxProviderError, TimeoutError) as error:
            raise _provider_platform_error(
                error,
                code="SANDBOX_PROCESS_FAILED",
                message="The Sandbox process could not be started.",
            ) from error
        return SandboxProcessResponse(
            sandbox_id=str(record.id),
            process_id=observation.process_id,
            status=_process_status(observation),
        )

    async def cancel_process(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: str,
        process_id: str,
        request: SandboxProcessControlRequest,
    ) -> SandboxProcessActionResponse:
        record, policy = await self._process_ready_instance(access, sandbox_id, request)
        assert record.provider_ref is not None
        try:
            observation = await self._provider_call(
                self._provider.cancel_process(
                    record.provider_ref,
                    process_id=process_id,
                    grace_seconds=_termination_grace_seconds(policy),
                )
            )
        except (SandboxProviderError, TimeoutError) as error:
            raise _provider_platform_error(
                error,
                code="SANDBOX_CANCEL_NOT_CONFIRMED",
                message="Sandbox process cancellation could not be confirmed.",
            ) from error
        return SandboxProcessActionResponse(
            sandbox_id=str(record.id),
            process_id=observation.process_id,
            status=_process_status(observation),
            changed=observation.state in {"CANCELLING", "CANCELLED", "TERMINATED"},
        )

    async def terminate(
        self, access: SandboxServiceAccess, *, sandbox_id: str
    ) -> SandboxActionResponse:
        record, changed = await self._begin_termination(access, sandbox_id)
        if record.provider_ref is None:
            return SandboxActionResponse(
                sandbox_id=str(record.id),
                action="terminate",
                status=record.status,
                changed=changed,
            )
        policy = _stored_policy(record)
        try:
            observation = await self._provider_call(
                self._provider.terminate(
                    record.provider_ref,
                    grace_seconds=_termination_grace_seconds(policy),
                )
            )
        except (SandboxProviderError, TimeoutError) as error:
            raise _provider_platform_error(
                error,
                code="SANDBOX_CANCEL_NOT_CONFIRMED",
                message="Sandbox termination could not be confirmed.",
            ) from error
        if observation.state == "TERMINATED":
            record = await self._store.finish_termination(
                access,
                sandbox_id=record.id,
                status="TERMINATED",
                provider_ref=observation.provider_ref,
                failure_code=None,
                now=self._now(),
            )
        return SandboxActionResponse(
            sandbox_id=str(record.id),
            action="terminate",
            status=record.status,
            changed=changed,
        )

    async def release(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: str,
        request: SandboxReleaseRequest,
    ) -> SandboxActionResponse:
        access.authorize()
        outcome = await self._store.release_lease(
            access,
            sandbox_id=_uuid(sandbox_id, "sandbox_id"),
            request=request,
            now=self._now(),
        )
        if outcome is None:
            raise resource_not_found("Sandbox was not found.")
        record, changed = outcome
        return SandboxActionResponse(
            sandbox_id=str(record.id),
            action="release",
            status=record.status,
            changed=changed,
        )

    async def destroy(
        self, access: SandboxServiceAccess, *, sandbox_id: str
    ) -> SandboxActionResponse:
        record, changed = await self._begin_termination(access, sandbox_id)
        if record.status == "TERMINATED":
            return SandboxActionResponse(
                sandbox_id=str(record.id),
                action="destroy",
                status="TERMINATED",
                changed=False,
            )
        if record.provider_ref is None:
            try:
                recovered = await self._provider_call(
                    self._provider.recover(str(record.id))
                )
            except (SandboxProviderError, TimeoutError) as error:
                await self._store.finish_termination(
                    access,
                    sandbox_id=record.id,
                    status="QUARANTINED",
                    provider_ref=None,
                    failure_code="SANDBOX_RECOVERY_FAILED",
                    now=self._now(),
                )
                raise _provider_platform_error(
                    error,
                    code="SANDBOX_CLEANUP_FAILED",
                    message="Sandbox recovery before cleanup could not be confirmed.",
                ) from error
            if recovered is not None:
                record = await self._store.finish_termination(
                    access,
                    sandbox_id=record.id,
                    status=(
                        "TERMINATED"
                        if recovered.state == "TERMINATED"
                        else "QUARANTINED"
                    ),
                    provider_ref=recovered.provider_ref,
                    failure_code=(
                        None
                        if recovered.state == "TERMINATED"
                        else "SANDBOX_RECOVERED_FOR_RECONCILIATION"
                    ),
                    now=self._now(),
                )
                return SandboxActionResponse(
                    sandbox_id=str(record.id),
                    action="destroy",
                    status=record.status,
                    changed=changed,
                )
            record = await self._store.finish_termination(
                access,
                sandbox_id=record.id,
                status="TERMINATED",
                provider_ref=None,
                failure_code=None,
                now=self._now(),
            )
            return SandboxActionResponse(
                sandbox_id=str(record.id),
                action="destroy",
                status=record.status,
                changed=changed,
            )
        try:
            observation = await self._provider_call(
                self._provider.destroy(record.provider_ref)
            )
        except (SandboxProviderError, TimeoutError) as error:
            await self._store.finish_termination(
                access,
                sandbox_id=record.id,
                status="QUARANTINED",
                provider_ref=record.provider_ref,
                failure_code="SANDBOX_CLEANUP_FAILED",
                now=self._now(),
            )
            raise _provider_platform_error(
                error,
                code="SANDBOX_CLEANUP_FAILED",
                message="Sandbox cleanup could not be confirmed.",
            ) from error
        terminal = observation.state == "TERMINATED"
        record = await self._store.finish_termination(
            access,
            sandbox_id=record.id,
            status="TERMINATED" if terminal else "QUARANTINED",
            provider_ref=observation.provider_ref,
            failure_code=None if terminal else "SANDBOX_CLEANUP_NOT_CONFIRMED",
            now=self._now(),
        )
        return SandboxActionResponse(
            sandbox_id=str(record.id),
            action="destroy",
            status=record.status,
            changed=changed,
        )

    async def _required_instance(
        self, access: SandboxServiceAccess, sandbox_id: str
    ) -> SandboxInstanceRecord:
        access.authorize()
        record = await self._store.get_instance(
            access, sandbox_id=_uuid(sandbox_id, "sandbox_id")
        )
        if record is None:
            raise resource_not_found("Sandbox was not found.")
        return record

    async def _process_ready_instance(
        self,
        access: SandboxServiceAccess,
        sandbox_id: str,
        request: SandboxLeaseControlRequest,
    ) -> tuple[SandboxInstanceRecord, FrozenSandboxPolicy]:
        access.authorize()
        record = await self._store.authorize_lease_control(
            access,
            sandbox_id=_uuid(sandbox_id, "sandbox_id"),
            request=request,
            now=self._now(),
        )
        if record is None:
            raise resource_not_found("Sandbox was not found.")
        if record.status != "IN_USE" or record.provider_ref is None:
            raise _sandbox_error(
                409,
                "SANDBOX_NOT_READY",
                "Sandbox requires an active Lease before process control.",
            )
        return record, _stored_policy(record)

    async def _begin_termination(
        self, access: SandboxServiceAccess, sandbox_id: str
    ) -> tuple[SandboxInstanceRecord, bool]:
        access.authorize()
        outcome = await self._store.begin_termination(
            access,
            sandbox_id=_uuid(sandbox_id, "sandbox_id"),
            now=self._now(),
        )
        if outcome is None:
            raise resource_not_found("Sandbox was not found.")
        return outcome

    async def _provider_call(
        self, awaitable: Awaitable[ProviderResult]
    ) -> ProviderResult:
        async with asyncio.timeout(self._provider_timeout_seconds):
            return await awaitable


def _provision_request_hash(request: SandboxProvisionRequest) -> str:
    payload = request.model_dump(mode="json", exclude={"provision_token"})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _provision_outcome(
    observation: ProviderSandboxObservation,
) -> tuple[
    SandboxStatus,
    Literal["RUNNING", "SUCCEEDED", "FAILED"],
    str | None,
]:
    if observation.state == "READY":
        return "READY", "SUCCEEDED", None
    if observation.state == "PROVISIONING":
        return "PROVISIONING", "RUNNING", None
    if observation.state == "FAILED":
        return "FAILED", "FAILED", "SANDBOX_PROVISION_FAILED"
    return "FAILED", "FAILED", "SANDBOX_PROVIDER_STATE_INVALID"


def _stored_policy(record: SandboxInstanceRecord) -> FrozenSandboxPolicy:
    policy = compile_sandbox_policy(record.policy_json)
    if policy.policy_hash != record.policy_hash:
        raise RuntimeError("Stored Sandbox policy hash does not match its snapshot")
    return policy


def _termination_grace_seconds(policy: FrozenSandboxPolicy) -> int:
    configured = policy.load().process.termination_grace_seconds
    return configured if configured is not None else 10


def _detail_response(record: SandboxInstanceRecord) -> SandboxDetailResponse:
    return SandboxDetailResponse(
        sandbox_id=str(record.id),
        status=record.status,
        scope=record.scope,
        image_digest=record.image_digest,
        policy_hash=record.policy_hash,
        bundle_hash=record.bundle_hash,
        workspace_uri=record.workspace_uri,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _inspect_response(
    record: SandboxInstanceRecord,
    lease: SandboxLeaseRecord | None,
    observation: ProviderSandboxObservation | None,
    *,
    now: datetime,
) -> SandboxInspectResponse:
    usage = None
    main_process = None
    observed_at = record.updated_at
    if observation is not None:
        observed_at = observation.observed_at
        if any(
            value is not None
            for value in (
                observation.cpu_seconds,
                observation.memory_bytes,
                observation.disk_bytes,
                observation.pids_current,
            )
        ):
            usage = SandboxResourceUsage(
                cpu_seconds=observation.cpu_seconds or 0,
                memory_bytes=observation.memory_bytes or 0,
                disk_bytes=observation.disk_bytes or 0,
                pids_current=observation.pids_current or 0,
                sampled_at=observation.observed_at,
            )
        if observation.main_process is not None:
            main_process = SandboxMainProcess(
                process_id=observation.main_process.process_id,
                status=_process_status(observation.main_process),
                exit_code=observation.main_process.exit_code,
            )
    return SandboxInspectResponse(
        sandbox_id=str(record.id),
        status=record.status,
        resource_usage=usage,
        main_process=main_process,
        lease=_lease_response(lease) if lease is not None else None,
        network_policy_hash=record.policy_hash,
        bundle_hash=record.bundle_hash,
        provider_observed_at=min(observed_at, now),
    )


def _lease_response(record: SandboxLeaseRecord) -> SandboxLeaseResponse:
    return SandboxLeaseResponse(
        sandbox_id=str(record.sandbox_id),
        lease_id=str(record.id),
        run_id=str(record.holder_run_id),
        execution_attempt=record.execution_attempt,
        execution_fencing_token_hash=record.fencing_token_hash,
        acquired_at=record.acquired_at,
        expires_at=record.expires_at,
    )


def _process_status(
    observation: ProviderProcessObservation,
) -> Literal[
    "STARTING",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "CANCELLING",
    "CANCELLED",
    "TERMINATED",
]:
    if observation.state == "UNKNOWN":
        raise _sandbox_error(
            409,
            "SANDBOX_PROCESS_FAILED",
            "The Sandbox Provider returned an unknown process state.",
        )
    return observation.state


def _require_request_tenant(access: SandboxServiceAccess, tenant_id: str) -> None:
    if tenant_id != access.context.tenant_id:
        raise permission_denied("Cross-tenant Sandbox access is not allowed.")


def _require_run_workspace_binding(request: SandboxProvisionRequest) -> None:
    try:
        actual = WorkspaceUri.parse(request.workspace_uri)
        expected = WorkspaceUri.root(
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            session_id=request.session_id,
            run_id=request.run_id,
        )
    except ValueError as error:
        raise _sandbox_error(
            400,
            "WORKSPACE_URI_INVALID",
            "The Run Workspace URI is invalid or non-canonical.",
        ) from error
    if actual != expected:
        raise _sandbox_error(
            400,
            "WORKSPACE_URI_INVALID",
            "The Run Workspace URI is not bound to the provision identity.",
        )


def _require_workspace_child(root: str, candidate: str) -> None:
    try:
        parsed_root = WorkspaceUri.parse(root)
        parsed_candidate = WorkspaceUri.parse(candidate)
    except ValueError as error:
        raise _sandbox_error(
            400,
            "WORKSPACE_URI_INVALID",
            "The process working directory is invalid or non-canonical.",
        ) from error
    if not parsed_root.is_within(parsed_candidate):
        raise _sandbox_error(
            400,
            "WORKSPACE_URI_INVALID",
            "The process working directory is outside the Sandbox Workspace.",
        )


def _uuid(value: str, field_name: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as error:
        raise _sandbox_error(
            400,
            "VALIDATION_ERROR",
            f"{field_name} must be a UUID.",
        ) from error


def _safe_operation_error(code: str) -> dict[str, object]:
    return {
        "code": code,
        "message": "Sandbox provisioning did not complete successfully.",
        "retryable": code in {"SANDBOX_PROVIDER_TIMEOUT", "SANDBOX_PROVISION_FAILED"},
    }


def _provider_platform_error(
    error: SandboxProviderError | TimeoutError,
    *,
    code: str,
    message: str,
) -> PlatformError:
    return _sandbox_error(
        503 if isinstance(error, TimeoutError) or error.retryable else 409,
        code,
        message,
        retryable=isinstance(error, TimeoutError) or error.retryable,
    )


def _sandbox_error(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> PlatformError:
    return PlatformError(
        status_code=status_code,
        code=code,
        message=message,
        retryable=retryable,
    )

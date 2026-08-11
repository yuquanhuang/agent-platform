"""Sandbox lifecycle orchestration tests over fake external boundaries."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID

import pytest
from pydantic import SecretStr

from packages.application.sandbox import (
    SANDBOX_MANAGE_PERMISSION,
    FrozenSandboxPolicy,
    ProviderProcessObservation,
    ProviderProvisionSpec,
    ProviderSandboxObservation,
    SandboxLifecycleService,
    SandboxLifecycleStore,
    SandboxPolicyResolver,
    SandboxProvider,
    SandboxProviderError,
    SandboxProvisionClaim,
    SandboxProvisionTokenVerifier,
    SandboxServiceAccess,
    compile_sandbox_policy,
)
from packages.contracts.public import PlatformError, SubjectType, TenantContext
from packages.contracts.sandbox_api import (
    SandboxOperationAccepted,
    SandboxProcessControlRequest,
    SandboxProcessRequest,
    SandboxProvisionRequest,
    SandboxReleaseRequest,
)
from packages.domain.public import (
    SandboxInstanceRecord,
    SandboxLeaseRecord,
    SandboxStatus,
)

NOW = datetime(2026, 8, 9, tzinfo=UTC)
TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
SERVICE_ID = UUID("22222222-2222-4222-8222-222222222222")
USER_ID = UUID("33333333-3333-4333-8333-333333333333")
SESSION_ID = UUID("44444444-4444-4444-8444-444444444444")
RUN_ID = UUID("55555555-5555-4555-8555-555555555555")
SANDBOX_ID = UUID("66666666-6666-4666-8666-666666666666")
OPERATION_ID = UUID("77777777-7777-4777-8777-777777777777")
IDEMPOTENCY_ID = UUID("88888888-8888-4888-8888-888888888888")
LEASE_ID = UUID("99999999-9999-4999-8999-999999999999")
BUNDLE_HASH = "sha256:" + "b" * 64


def policy() -> FrozenSandboxPolicy:
    return compile_sandbox_policy(
        {
            "schema_version": "1.0",
            "scope": "run",
            "image_digest": "runtime@sha256:" + "a" * 64,
            "cpu_limit": 1,
            "memory_mb": 512,
            "disk_mb": 1024,
            "pids_limit": 64,
            "timeout_seconds": 600,
            "network": {
                "mode": "none",
                "allow_domains": [],
                "allow_ports": [],
                "deny_private_networks": True,
            },
            "filesystem": {
                "read_patterns": ["work/**"],
                "write_patterns": ["work/**"],
                "max_files": 100,
                "max_file_bytes": 1024,
            },
            "process": {
                "allowed_executables": ["python"],
                "shell_allowed": False,
                "max_processes": 16,
                "termination_grace_seconds": 1,
            },
            "artifacts": {
                "allow_export": False,
                "max_artifacts": 0,
                "max_total_bytes": 0,
                "allowed_content_types": [],
            },
        }
    )


def access() -> SandboxServiceAccess:
    return SandboxServiceAccess(
        context=TenantContext(
            tenant_id=str(TENANT_ID),
            subject_type=SubjectType.SERVICE,
            subject_id=str(SERVICE_ID),
            auth_time=NOW,
            request_id="req_sandbox",
            trace_id="trace_sandbox",
        ),
        permissions=frozenset({SANDBOX_MANAGE_PERMISSION}),
    )


def request(*, scope: Literal["run", "session"] = "run") -> SandboxProvisionRequest:
    snapshot = policy()
    return SandboxProvisionRequest(
        tenant_id=str(TENANT_ID),
        user_id=str(USER_ID),
        session_id=str(SESSION_ID),
        run_id=str(RUN_ID),
        execution_attempt=1,
        scope=scope,
        policy_ref="immutable://sandbox-policy/policy_001",
        policy_hash=snapshot.policy_hash,
        bundle_ref=(
            f"bundle://tenant/{TENANT_ID}/snapshot/snp_001/runtime/agentscope/"
            f"{BUNDLE_HASH}"
        ),
        bundle_hash=BUNDLE_HASH,
        workspace_uri=(
            f"workspace://tenant/{TENANT_ID}/user/{USER_ID}/session/{SESSION_ID}/"
            f"runs/{RUN_ID}/"
        ),
        provision_token=SecretStr("one-time-provision-token"),
        trace_id="trace_sandbox",
    )


def instance(
    *, status: SandboxStatus = "REQUESTED", provider_ref: str | None = None
) -> SandboxInstanceRecord:
    snapshot = policy()
    return SandboxInstanceRecord(
        id=SANDBOX_ID,
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        execution_attempt=1,
        scope="run",
        image_digest=snapshot.load().image_digest,
        policy_ref="immutable://sandbox-policy/policy_001",
        policy_hash=snapshot.policy_hash,
        policy_schema_version=snapshot.schema_version,
        policy_json=snapshot.load().model_dump(mode="json"),
        bundle_ref=(
            f"bundle://tenant/{TENANT_ID}/snapshot/snp_001/runtime/agentscope/"
            f"{BUNDLE_HASH}"
        ),
        bundle_hash=BUNDLE_HASH,
        workspace_uri=(
            f"workspace://tenant/{TENANT_ID}/user/{USER_ID}/session/{SESSION_ID}/"
            f"runs/{RUN_ID}/"
        ),
        runtime_target_id="agentscope-default",
        status=status,
        provider_ref=provider_ref,
        lease_expires_at=None,
        provision_operation_id=OPERATION_ID,
        created_at=NOW,
        updated_at=NOW,
        terminated_at=None,
        failure_code=None,
    )


def lease() -> SandboxLeaseRecord:
    return SandboxLeaseRecord(
        id=LEASE_ID,
        tenant_id=TENANT_ID,
        sandbox_id=SANDBOX_ID,
        holder_run_id=RUN_ID,
        execution_attempt=1,
        fencing_token_hash="sha256:" + "c" * 64,
        acquired_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        released_at=None,
    )


def control_request() -> SandboxReleaseRequest:
    return SandboxReleaseRequest(
        run_id=str(RUN_ID),
        execution_attempt=1,
        execution_fencing_token=SecretStr("execution-fencing-token"),
        trace_id="trace_sandbox",
    )


class StoreFake:
    def __init__(self) -> None:
        self.record = instance()
        self.active_lease: SandboxLeaseRecord | None = None
        self.replayed = False
        self.finish_provision_calls: list[dict[str, object]] = []
        self.finish_termination_calls: list[dict[str, object]] = []

    async def begin_provision(self, *args: object, **kwargs: object):
        del args, kwargs
        accepted = SandboxOperationAccepted(
            sandbox_id=str(SANDBOX_ID),
            operation_id=str(OPERATION_ID),
            status_url=f"/api/v1/operations/{OPERATION_ID}",
        )
        return SandboxProvisionClaim(
            accepted=accepted,
            instance=None if self.replayed else self.record,
            idempotency_record_id=None if self.replayed else IDEMPOTENCY_ID,
            replayed=self.replayed,
        )

    async def mark_provisioning(self, *args: object, **kwargs: object):
        del args, kwargs
        self.record = instance(status="PROVISIONING")
        return self.record

    async def finish_provision(self, *args: object, **kwargs: object):
        del args
        self.finish_provision_calls.append(kwargs)
        self.record = instance(
            status=cast(SandboxStatus, kwargs["status"]),
            provider_ref=cast(str | None, kwargs["provider_ref"]),
        )
        return self.record

    async def get_instance(self, *args: object, **kwargs: object):
        del args, kwargs
        return self.record

    async def get_active_lease(self, *args: object, **kwargs: object):
        del args, kwargs
        return self.active_lease

    async def acquire_lease(self, *args: object, **kwargs: object):
        del args, kwargs
        return self.active_lease

    async def authorize_lease_control(self, *args: object, **kwargs: object):
        del args, kwargs
        if self.active_lease is None:
            raise PlatformError(
                status_code=409,
                code="SANDBOX_LEASE_CONFLICT",
                message="missing lease",
            )
        return self.record

    async def release_lease(self, *args: object, **kwargs: object):
        del args, kwargs
        return self.record, True

    async def begin_termination(self, *args: object, **kwargs: object):
        del args, kwargs
        self.record = instance(
            status="TERMINATING", provider_ref=self.record.provider_ref
        )
        return self.record, True

    async def finish_termination(self, *args: object, **kwargs: object):
        del args
        self.finish_termination_calls.append(kwargs)
        self.record = instance(
            status=cast(SandboxStatus, kwargs["status"]),
            provider_ref=cast(str | None, kwargs["provider_ref"]),
        )
        return self.record


class ProviderFake:
    def __init__(self) -> None:
        self.provision_spec: ProviderProvisionSpec | None = None
        self.provision_error: Exception | None = None
        self.recover_error: Exception | None = None
        self.cancel_grace_seconds: int | None = None

    async def provision(self, spec: ProviderProvisionSpec):
        self.provision_spec = spec
        if self.provision_error is not None:
            raise self.provision_error
        return ProviderSandboxObservation(
            provider_ref="provider://sandbox/001",
            state="READY",
            observed_at=NOW,
        )

    async def inspect(self, provider_ref: str):
        return ProviderSandboxObservation(
            provider_ref=provider_ref,
            state="RUNNING",
            observed_at=NOW,
        )

    async def recover(self, sandbox_id: str) -> None:
        del sandbox_id
        if self.recover_error is not None:
            raise self.recover_error

    async def start_process(self, provider_ref: str, **kwargs: object):
        del provider_ref, kwargs
        return ProviderProcessObservation(process_id="proc_001", state="STARTING")

    async def cancel_process(self, provider_ref: str, **kwargs: object):
        del provider_ref
        self.cancel_grace_seconds = cast(int, kwargs["grace_seconds"])
        return ProviderProcessObservation(process_id="proc_001", state="CANCELLED")

    async def terminate(self, provider_ref: str, **kwargs: object):
        del kwargs
        return ProviderSandboxObservation(
            provider_ref=provider_ref,
            state="TERMINATED",
            observed_at=NOW,
        )

    async def destroy(self, provider_ref: str):
        return ProviderSandboxObservation(
            provider_ref=provider_ref,
            state="TERMINATED",
            observed_at=NOW,
        )


class ResolverFake:
    def __init__(self, resolved: FrozenSandboxPolicy | None = None) -> None:
        self.resolved = resolved if resolved is not None else policy()

    async def resolve(self, *args: object, **kwargs: object):
        del args, kwargs
        return self.resolved


class VerifierFake:
    def __init__(self, error: PlatformError | None = None) -> None:
        self.error = error
        self.calls = 0

    async def verify(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.calls += 1
        if self.error is not None:
            raise self.error


def service(
    store: StoreFake,
    provider: ProviderFake,
    *,
    verifier: VerifierFake | None = None,
    provider_timeout_seconds: float = 1,
) -> SandboxLifecycleService:
    return SandboxLifecycleService(
        cast(SandboxLifecycleStore, store),
        cast(SandboxProvider, provider),
        cast(SandboxPolicyResolver, ResolverFake()),
        cast(SandboxProvisionTokenVerifier, verifier or VerifierFake()),
        provider_timeout_seconds=provider_timeout_seconds,
        now=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_provision_persists_success_and_provider_receives_no_secret() -> None:
    store = StoreFake()
    provider = ProviderFake()

    accepted = await service(store, provider).provision(
        access(), request=request(), idempotency_key="sandbox/key"
    )

    assert accepted.sandbox_id == str(SANDBOX_ID)
    assert provider.provision_spec is not None
    assert provider.provision_spec.bundle_hash == BUNDLE_HASH
    assert not hasattr(provider.provision_spec, "provision_token")
    assert store.finish_provision_calls[-1]["status"] == "READY"
    assert store.finish_provision_calls[-1]["operation_status"] == "SUCCEEDED"


@pytest.mark.asyncio
async def test_replay_returns_original_operation_without_calling_provider() -> None:
    store = StoreFake()
    store.replayed = True
    provider = ProviderFake()

    accepted = await service(store, provider).provision(
        access(), request=request(), idempotency_key="sandbox/key"
    )

    assert accepted.operation_id == str(OPERATION_ID)
    assert provider.provision_spec is None


@pytest.mark.asyncio
async def test_token_failure_prevents_store_and_provider_side_effects() -> None:
    store = StoreFake()
    provider = ProviderFake()
    verifier = VerifierFake(
        PlatformError(status_code=403, code="TOKEN_INVALID", message="invalid")
    )

    with pytest.raises(PlatformError, match="invalid"):
        await service(store, provider, verifier=verifier).provision(
            access(), request=request(), idempotency_key="sandbox/key"
        )

    assert verifier.calls == 1
    assert store.finish_provision_calls == []
    assert provider.provision_spec is None


@pytest.mark.asyncio
async def test_session_scope_is_rejected_before_token_verification() -> None:
    verifier = VerifierFake()

    with pytest.raises(PlatformError) as captured:
        await service(StoreFake(), ProviderFake(), verifier=verifier).provision(
            access(), request=request(scope="session"), idempotency_key="sandbox/key"
        )

    assert captured.value.code == "SANDBOX_POLICY_DENIED"
    assert verifier.calls == 0


@pytest.mark.asyncio
async def test_provision_workspace_must_match_tenant_user_session_and_run() -> None:
    verifier = VerifierFake()
    invalid = request().model_copy(
        update={"workspace_uri": f"workspace://tenant/{TENANT_ID}/other-run/"}
    )

    with pytest.raises(PlatformError) as captured:
        await service(StoreFake(), ProviderFake(), verifier=verifier).provision(
            access(), request=invalid, idempotency_key="sandbox/key"
        )

    assert captured.value.code == "WORKSPACE_URI_INVALID"
    assert verifier.calls == 0


@pytest.mark.asyncio
async def test_provider_timeout_finishes_operation_as_failed() -> None:
    class SlowProvider(ProviderFake):
        async def provision(self, spec: ProviderProvisionSpec):
            self.provision_spec = spec
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    store = StoreFake()
    provider = SlowProvider()

    await service(store, provider, provider_timeout_seconds=0.001).provision(
        access(), request=request(), idempotency_key="sandbox/key"
    )

    assert store.finish_provision_calls[-1]["status"] == "FAILED"
    assert (
        store.finish_provision_calls[-1]["failure_code"] == "SANDBOX_PROVIDER_TIMEOUT"
    )


@pytest.mark.asyncio
async def test_process_requires_active_lease_and_enforces_workspace() -> None:
    store = StoreFake()
    store.record = instance(status="IN_USE", provider_ref="provider://sandbox/001")
    provider = ProviderFake()
    lifecycle = service(store, provider)
    process_request = SandboxProcessRequest(
        run_id=str(RUN_ID),
        execution_attempt=1,
        execution_fencing_token=SecretStr("execution-fencing-token"),
        process_id="proc_001",
        argv=["python", "-m", "runtime_entry"],
        working_directory=store.record.workspace_uri + "work/",
        timeout_seconds=30,
        trace_id="trace_sandbox",
    )

    with pytest.raises(PlatformError) as missing_lease:
        await lifecycle.start_process(
            access(), sandbox_id=str(SANDBOX_ID), request=process_request
        )
    assert missing_lease.value.code == "SANDBOX_LEASE_CONFLICT"

    store.active_lease = lease()
    escaped = process_request.model_copy(
        update={"working_directory": f"workspace://tenant/{TENANT_ID}/other/"}
    )
    with pytest.raises(PlatformError) as invalid_workspace:
        await lifecycle.start_process(
            access(), sandbox_id=str(SANDBOX_ID), request=escaped
        )
    assert invalid_workspace.value.code == "WORKSPACE_URI_INVALID"


@pytest.mark.asyncio
async def test_configured_termination_grace_is_forwarded_to_provider() -> None:
    store = StoreFake()
    store.record = instance(status="IN_USE", provider_ref="provider://sandbox/001")
    store.active_lease = lease()
    provider = ProviderFake()

    await service(store, provider).cancel_process(
        access(),
        sandbox_id=str(SANDBOX_ID),
        process_id="proc_001",
        request=SandboxProcessControlRequest.model_validate(
            control_request().model_dump()
        ),
    )

    assert provider.cancel_grace_seconds == 1


@pytest.mark.asyncio
async def test_destroy_recovery_failure_quarantines_for_reconciliation() -> None:
    store = StoreFake()
    provider = ProviderFake()
    provider.recover_error = SandboxProviderError(
        code="PROVIDER_UNAVAILABLE",
        message="unavailable",
        retryable=True,
    )

    with pytest.raises(PlatformError) as captured:
        await service(store, provider).destroy(access(), sandbox_id=str(SANDBOX_ID))

    assert captured.value.code == "SANDBOX_CLEANUP_FAILED"
    assert store.finish_termination_calls[-1]["status"] == "QUARANTINED"
    assert (
        store.finish_termination_calls[-1]["failure_code"] == "SANDBOX_RECOVERY_FAILED"
    )

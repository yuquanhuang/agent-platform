"""Sandbox Manager route, workload identity, and error-envelope tests."""

import json
import re
from datetime import UTC, datetime
from typing import cast

import httpx
import pytest

from apps.sandbox_manager.app import create_sandbox_manager_app
from packages.application.sandbox import (
    SANDBOX_MANAGE_PERMISSION,
    SandboxInternalService,
    SandboxServiceAccess,
)
from packages.contracts.public import SubjectType, TenantContext
from packages.contracts.sandbox_api import (
    SandboxActionResponse,
    SandboxDetailResponse,
    SandboxInspectResponse,
    SandboxLeaseControlRequest,
    SandboxLeaseResponse,
    SandboxOperationAccepted,
    SandboxProcessActionResponse,
    SandboxProcessResponse,
)

TENANT_ID = "11111111-1111-4111-8111-111111111111"
SERVICE_ID = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 8, 9, tzinfo=UTC)
HASH = "sha256:" + "a" * 64
IMAGE = "runtime@sha256:" + "b" * 64


class IdentityStub:
    def __init__(
        self,
        *,
        subject_type: SubjectType = SubjectType.SERVICE,
        permissions: frozenset[str] = frozenset({SANDBOX_MANAGE_PERMISSION}),
    ) -> None:
        self.subject_type = subject_type
        self.permissions = permissions
        self.calls = 0

    async def authenticate(self, request: object) -> SandboxServiceAccess:
        del request
        self.calls += 1
        return SandboxServiceAccess(
            context=TenantContext(
                tenant_id=TENANT_ID,
                subject_type=self.subject_type,
                subject_id=SERVICE_ID,
                membership_version=(
                    1 if self.subject_type is SubjectType.USER else None
                ),
                auth_time=NOW,
                request_id="req_sandbox",
                trace_id="trace_sandbox",
            ),
            permissions=self.permissions,
        )


class ServiceStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def provision(self, access: object, **kwargs: object):
        del access
        self.calls.append(("provision", kwargs))
        return SandboxOperationAccepted(
            sandbox_id="sbx_001",
            operation_id="op_001",
            status_url="/api/v1/operations/op_001",
        )

    async def get(self, access: object, **kwargs: object):
        del access
        self.calls.append(("get", kwargs))
        return SandboxDetailResponse(
            sandbox_id="sbx_001",
            status="READY",
            scope="run",
            image_digest=IMAGE,
            policy_hash=HASH,
            bundle_hash=HASH,
            workspace_uri="workspace://tenant/ten_001/runs/run_001/",
            created_at=NOW,
            updated_at=NOW,
        )

    async def inspect(self, access: object, **kwargs: object):
        del access
        self.calls.append(("inspect", kwargs))
        return SandboxInspectResponse(
            sandbox_id="sbx_001",
            status="READY",
            network_policy_hash=HASH,
            bundle_hash=HASH,
            provider_observed_at=NOW,
        )

    async def acquire_lease(self, access: object, **kwargs: object):
        del access
        self.calls.append(("acquire_lease", kwargs))
        return SandboxLeaseResponse(
            sandbox_id="sbx_001",
            lease_id="lease_001",
            run_id="run_001",
            execution_attempt=1,
            execution_fencing_token_hash=HASH,
            acquired_at=NOW,
            expires_at=datetime(2026, 8, 9, 0, 5, tzinfo=UTC),
        )

    async def start_process(self, access: object, **kwargs: object):
        del access
        self.calls.append(("start_process", kwargs))
        return SandboxProcessResponse(
            sandbox_id="sbx_001", process_id="proc_001", status="STARTING"
        )

    async def cancel_process(self, access: object, **kwargs: object):
        del access
        self.calls.append(("cancel_process", kwargs))
        return SandboxProcessActionResponse(
            sandbox_id="sbx_001",
            process_id="proc_001",
            status="CANCELLING",
            changed=True,
        )

    async def terminate(self, access: object, **kwargs: object):
        del access
        self.calls.append(("terminate", kwargs))
        return SandboxActionResponse(
            sandbox_id="sbx_001",
            action="terminate",
            status="TERMINATING",
            changed=True,
        )

    async def release(self, access: object, **kwargs: object):
        del access
        self.calls.append(("release", kwargs))
        return SandboxActionResponse(
            sandbox_id="sbx_001",
            action="release",
            status="TERMINATING",
            changed=True,
        )

    async def destroy(self, access: object, **kwargs: object):
        del access
        self.calls.append(("destroy", kwargs))
        return SandboxActionResponse(
            sandbox_id="sbx_001",
            action="destroy",
            status="TERMINATED",
            changed=True,
        )


def provision_payload() -> dict[str, object]:
    return {
        "tenant_id": "ten_001",
        "user_id": "usr_001",
        "session_id": "ses_001",
        "run_id": "run_001",
        "execution_attempt": 1,
        "scope": "run",
        "policy_ref": "immutable://sandbox-policy/policy_001",
        "policy_hash": HASH,
        "bundle_ref": "bundle://tenant/ten_001/bundle_001",
        "bundle_hash": HASH,
        "workspace_uri": "workspace://tenant/ten_001/runs/run_001/",
        "provision_token": "one-time-provision-token",
        "trace_id": "trace_001",
    }


def build_app(identity: IdentityStub | None = None, service: ServiceStub | None = None):
    return create_sandbox_manager_app(
        identity_provider=identity,
        service=cast(SandboxInternalService | None, service),
    )


def test_openapi_registers_only_the_frozen_internal_operations() -> None:
    operation_ids = set(
        re.findall(
            r'"operationId":\s*"([^"]+)"',
            json.dumps(build_app(IdentityStub(), ServiceStub()).openapi()),
        )
    )

    assert operation_ids == {
        "provisionSandbox",
        "getSandbox",
        "inspectSandbox",
        "acquireSandboxLease",
        "startSandboxProcess",
        "cancelSandboxProcess",
        "terminateSandbox",
        "releaseSandbox",
        "destroySandbox",
    }


@pytest.mark.asyncio
async def test_provision_authenticates_and_forwards_idempotency_key() -> None:
    identity = IdentityStub()
    service = ServiceStub()
    transport = httpx.ASGITransport(app=build_app(identity, service))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/internal/v1/sandboxes",
            json=provision_payload(),
            headers={"Idempotency-Key": f"sandbox/run_001/1/{HASH}"},
        )

    assert response.status_code == 202
    assert response.json()["status"] == "ACCEPTED"
    assert identity.calls == 1
    assert service.calls[0][0] == "provision"
    kwargs = cast(dict[str, object], service.calls[0][1])
    assert kwargs["idempotency_key"] == f"sandbox/run_001/1/{HASH}"


@pytest.mark.asyncio
async def test_provision_rejects_idempotency_key_not_bound_to_request() -> None:
    service = ServiceStub()
    transport = httpx.ASGITransport(app=build_app(IdentityStub(), service))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/internal/v1/sandboxes",
            json=provision_payload(),
            headers={"Idempotency-Key": "sandbox/run_other/1/sha256:invalid"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert service.calls == []


@pytest.mark.asyncio
async def test_lease_holder_operations_require_and_forward_fencing_proof() -> None:
    service = ServiceStub()
    transport = httpx.ASGITransport(app=build_app(IdentityStub(), service))
    control = {
        "run_id": "run_001",
        "execution_attempt": 1,
        "execution_fencing_token": "execution-fencing-token",
        "trace_id": "trace_001",
    }
    process = {
        **control,
        "process_id": "proc_001",
        "argv": ["python", "-m", "runtime_entry"],
        "working_directory": "workspace://tenant/ten_001/runs/run_001/work/",
        "timeout_seconds": 60,
    }
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        started = await client.post(
            "/internal/v1/sandboxes/sbx_001/processes", json=process
        )
        cancelled = await client.post(
            "/internal/v1/sandboxes/sbx_001/processes/proc_001/cancel",
            json=control,
        )
        released = await client.post(
            "/internal/v1/sandboxes/sbx_001/release", json=control
        )

    assert started.status_code == 200
    assert cancelled.status_code == 200
    assert released.status_code == 200
    assert [name for name, _ in service.calls] == [
        "start_process",
        "cancel_process",
        "release",
    ]
    for _, kwargs in service.calls:
        typed_kwargs = cast(dict[str, object], kwargs)
        body = cast(SandboxLeaseControlRequest, typed_kwargs["request"])
        assert "execution_fencing_token" in body.model_dump()
        assert "execution-fencing-token" not in body.model_dump_json()


@pytest.mark.asyncio
async def test_service_dependency_is_checked_after_workload_authentication() -> None:
    identity = IdentityStub()
    transport = httpx.ASGITransport(app=build_app(identity, None))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/internal/v1/sandboxes/sbx_001")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert identity.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity", "status_code", "error_code"),
    [
        (IdentityStub(subject_type=SubjectType.USER), 401, "UNAUTHENTICATED"),
        (IdentityStub(permissions=frozenset()), 403, "PERMISSION_DENIED"),
    ],
)
async def test_route_rejects_untrusted_or_unprivileged_identity_before_service(
    identity: IdentityStub, status_code: int, error_code: str
) -> None:
    service = ServiceStub()
    transport = httpx.ASGITransport(app=build_app(identity, service))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/internal/v1/sandboxes/sbx_001")

    assert response.status_code == status_code
    assert response.json()["error"]["code"] == error_code
    assert service.calls == []


@pytest.mark.asyncio
async def test_validation_error_does_not_echo_provision_token() -> None:
    payload = provision_payload()
    payload["execution_attempt"] = 0
    transport = httpx.ASGITransport(app=build_app(IdentityStub(), ServiceStub()))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/internal/v1/sandboxes",
            json=payload,
            headers={"Idempotency-Key": "sandbox/run_001/0/policy"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONTRACT_VALIDATION_FAILED"
    assert "one-time-provision-token" not in response.text

"""Strict internal Sandbox Manager routes with workload identity enforcement."""

from typing import Annotated, Protocol

from fastapi import APIRouter, Header, Request, status

from packages.application.sandbox import (
    SandboxInternalService,
    SandboxServiceAccess,
)
from packages.contracts.public import dependency_unavailable, validation_error
from packages.contracts.sandbox_api import (
    SandboxActionResponse,
    SandboxDetailResponse,
    SandboxInspectResponse,
    SandboxLeaseRequest,
    SandboxLeaseResponse,
    SandboxOperationAccepted,
    SandboxProcessActionResponse,
    SandboxProcessControlRequest,
    SandboxProcessRequest,
    SandboxProcessResponse,
    SandboxProvisionRequest,
    SandboxReleaseRequest,
)

IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=512,
    ),
]


class SandboxServiceIdentityProvider(Protocol):
    """Resolve a trusted mTLS/workload identity and tenant-scoped grants."""

    async def authenticate(self, request: Request) -> SandboxServiceAccess: ...


def create_sandbox_router(
    identity_provider: SandboxServiceIdentityProvider | None,
    service: SandboxInternalService | None,
) -> APIRouter:
    router = APIRouter(prefix="/internal/v1/sandboxes")

    async def authorized_access(request: Request) -> SandboxServiceAccess:
        if identity_provider is None:
            raise dependency_unavailable(
                "Sandbox workload identity authentication is not configured."
            )
        access = await identity_provider.authenticate(request)
        access.authorize()
        if service is None:
            raise dependency_unavailable("Sandbox Manager service is not configured.")
        return access

    @router.post(
        "",
        tags=["InternalSandboxes"],
        operation_id="provisionSandbox",
        response_model=SandboxOperationAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def provision_sandbox(  # pyright: ignore[reportUnusedFunction]
        body: SandboxProvisionRequest,
        request: Request,
        idempotency_key: IdempotencyKey,
    ) -> SandboxOperationAccepted:
        access = await authorized_access(request)
        assert service is not None
        expected_key = (
            f"sandbox/{body.run_id}/{body.execution_attempt}/{body.policy_hash}"
        )
        if idempotency_key != expected_key:
            raise validation_error(
                "Idempotency-Key does not match the Sandbox provision request."
            )
        return await service.provision(
            access,
            request=body,
            idempotency_key=idempotency_key,
        )

    @router.get(
        "/{sandbox_id}",
        tags=["InternalSandboxes"],
        operation_id="getSandbox",
        response_model=SandboxDetailResponse,
    )
    async def get_sandbox(  # pyright: ignore[reportUnusedFunction]
        sandbox_id: str,
        request: Request,
    ) -> SandboxDetailResponse:
        access = await authorized_access(request)
        assert service is not None
        return await service.get(access, sandbox_id=sandbox_id)

    @router.get(
        "/{sandbox_id}/inspect",
        tags=["InternalSandboxes"],
        operation_id="inspectSandbox",
        response_model=SandboxInspectResponse,
    )
    async def inspect_sandbox(  # pyright: ignore[reportUnusedFunction]
        sandbox_id: str,
        request: Request,
    ) -> SandboxInspectResponse:
        access = await authorized_access(request)
        assert service is not None
        return await service.inspect(access, sandbox_id=sandbox_id)

    @router.post(
        "/{sandbox_id}/leases",
        tags=["InternalSandboxes"],
        operation_id="acquireSandboxLease",
        response_model=SandboxLeaseResponse,
    )
    async def acquire_sandbox_lease(  # pyright: ignore[reportUnusedFunction]
        sandbox_id: str,
        body: SandboxLeaseRequest,
        request: Request,
    ) -> SandboxLeaseResponse:
        access = await authorized_access(request)
        assert service is not None
        return await service.acquire_lease(
            access,
            sandbox_id=sandbox_id,
            request=body,
        )

    @router.post(
        "/{sandbox_id}/processes",
        tags=["InternalSandboxes"],
        operation_id="startSandboxProcess",
        response_model=SandboxProcessResponse,
    )
    async def start_sandbox_process(  # pyright: ignore[reportUnusedFunction]
        sandbox_id: str,
        body: SandboxProcessRequest,
        request: Request,
    ) -> SandboxProcessResponse:
        access = await authorized_access(request)
        assert service is not None
        return await service.start_process(
            access,
            sandbox_id=sandbox_id,
            request=body,
        )

    @router.post(
        "/{sandbox_id}/processes/{process_id}/cancel",
        tags=["InternalSandboxes"],
        operation_id="cancelSandboxProcess",
        response_model=SandboxProcessActionResponse,
    )
    async def cancel_sandbox_process(  # pyright: ignore[reportUnusedFunction]
        sandbox_id: str,
        process_id: str,
        body: SandboxProcessControlRequest,
        request: Request,
    ) -> SandboxProcessActionResponse:
        access = await authorized_access(request)
        assert service is not None
        return await service.cancel_process(
            access,
            sandbox_id=sandbox_id,
            process_id=process_id,
            request=body,
        )

    @router.post(
        "/{sandbox_id}/terminate",
        tags=["InternalSandboxes"],
        operation_id="terminateSandbox",
        response_model=SandboxActionResponse,
    )
    async def terminate_sandbox(  # pyright: ignore[reportUnusedFunction]
        sandbox_id: str,
        request: Request,
    ) -> SandboxActionResponse:
        access = await authorized_access(request)
        assert service is not None
        return await service.terminate(access, sandbox_id=sandbox_id)

    @router.post(
        "/{sandbox_id}/release",
        tags=["InternalSandboxes"],
        operation_id="releaseSandbox",
        response_model=SandboxActionResponse,
    )
    async def release_sandbox(  # pyright: ignore[reportUnusedFunction]
        sandbox_id: str,
        body: SandboxReleaseRequest,
        request: Request,
    ) -> SandboxActionResponse:
        access = await authorized_access(request)
        assert service is not None
        return await service.release(access, sandbox_id=sandbox_id, request=body)

    @router.delete(
        "/{sandbox_id}",
        tags=["InternalSandboxes"],
        operation_id="destroySandbox",
        response_model=SandboxActionResponse,
    )
    async def destroy_sandbox(  # pyright: ignore[reportUnusedFunction]
        sandbox_id: str,
        request: Request,
    ) -> SandboxActionResponse:
        access = await authorized_access(request)
        assert service is not None
        return await service.destroy(access, sandbox_id=sandbox_id)

    return router

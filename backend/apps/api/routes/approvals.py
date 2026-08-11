"""Frozen Approval query and decision routes."""

from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Request, Response

from packages.application.public import (
    ApprovalManagementService,
    RequestMetadata,
    approval_etag,
)
from packages.contracts.generated.core_models import (
    Approval,
    ApprovalDecisionRequest,
    ApprovalPage,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    IdentityProvider,
    dependency_unavailable,
)

ApprovalQueryStatus = Literal[
    "PENDING", "APPROVED", "REJECTED", "EXPIRED", "CANCELLED", "CONSUMED"
]


def create_approval_router(
    identity_provider: IdentityProvider | None,
    service: ApprovalManagementService | None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    def metadata(request: Request) -> RequestMetadata:
        trace_id = getattr(request.state, "trace_id", None)
        request_id = getattr(request.state, "request_id", None)
        if not isinstance(trace_id, str) or not isinstance(request_id, str):
            raise TypeError("request correlation context is not installed")
        return RequestMetadata(request_id=request_id, trace_id=trace_id)

    def authenticate(
        authorization: str | None,
    ) -> tuple[AuthenticatedPrincipal, ApprovalManagementService]:
        if identity_provider is None or service is None:
            raise dependency_unavailable("Approval service is not configured.")
        return identity_provider.authenticate(authorization), service

    @router.get(
        "/approvals",
        tags=["Approvals"],
        operation_id="listApprovals",
        response_model=ApprovalPage,
    )
    async def list_approvals(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        cursor: Annotated[str | None, Query()] = None,
        status: Annotated[ApprovalQueryStatus | None, Query()] = None,
        run_id: Annotated[str | None, Query()] = None,
    ) -> ApprovalPage:
        principal, configured_service = authenticate(authorization)
        return await configured_service.list_approvals(
            principal,
            status=status,
            run_id=run_id,
            limit=limit,
            cursor=cursor,
            metadata=metadata(request),
        )

    @router.get(
        "/approvals/{approval_id}",
        tags=["Approvals"],
        operation_id="getApproval",
        response_model=Approval,
    )
    async def get_approval(  # pyright: ignore[reportUnusedFunction]
        approval_id: str,
        request: Request,
        response: Response,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Approval:
        principal, configured_service = authenticate(authorization)
        approval = await configured_service.get_approval(
            principal,
            approval_id=approval_id,
            metadata=metadata(request),
        )
        response.headers["ETag"] = approval_etag(approval)
        return approval

    @router.post(
        "/approvals/{approval_id}/decision",
        tags=["Approvals"],
        operation_id="decideApproval",
        response_model=Approval,
    )
    async def decide_approval(  # pyright: ignore[reportUnusedFunction]
        approval_id: str,
        request: Request,
        response: Response,
        body: ApprovalDecisionRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        if_match: Annotated[
            str, Header(alias="If-Match", pattern=r'^"rv:[1-9][0-9]*"$')
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Approval:
        principal, configured_service = authenticate(authorization)
        approval = await configured_service.decide_approval(
            principal,
            approval_id=approval_id,
            request=body,
            if_match=if_match,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )
        response.headers["ETag"] = approval_etag(approval)
        return approval

    return router

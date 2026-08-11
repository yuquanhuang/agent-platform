"""Frozen tenant AuditLog query route."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request

from packages.application.public import AuditManagementService, RequestMetadata
from packages.contracts.generated.resources_models import AuditPage
from packages.contracts.public import (
    AuthenticatedPrincipal,
    IdentityProvider,
    dependency_unavailable,
)


def create_audit_router(
    identity_provider: IdentityProvider | None,
    service: AuditManagementService | None,
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
    ) -> tuple[AuthenticatedPrincipal, AuditManagementService]:
        if identity_provider is None or service is None:
            raise dependency_unavailable("Audit service is not configured.")
        return identity_provider.authenticate(authorization), service

    @router.get(
        "/audit-logs",
        tags=["Audit"],
        operation_id="listAuditLogs",
        response_model=AuditPage,
    )
    async def list_audit_logs(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        cursor: Annotated[str | None, Query()] = None,
        action: Annotated[str | None, Query(max_length=128)] = None,
        resource_type: Annotated[str | None, Query(max_length=64)] = None,
        actor_id: Annotated[str | None, Query()] = None,
        run_id: Annotated[str | None, Query()] = None,
        occurred_from: Annotated[datetime | None, Query()] = None,
        occurred_to: Annotated[datetime | None, Query()] = None,
    ) -> AuditPage:
        principal, configured_service = authenticate(authorization)
        return await configured_service.list_audit_logs(
            principal,
            action=action,
            resource_type=resource_type,
            actor_id=actor_id,
            run_id=run_id,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            limit=limit,
            cursor=cursor,
            metadata=metadata(request),
        )

    return router

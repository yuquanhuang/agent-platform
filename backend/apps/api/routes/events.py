"""Internal service-authenticated RuntimeEventCandidate ingestion route."""

from typing import Protocol

from fastapi import APIRouter, Request

from packages.application.event_service import (
    EventWriteAccess,
    RunEventIngestionService,
)
from packages.contracts.generated.core_models import (
    RunEventBatchRequest,
    RunEventBatchResponse,
)
from packages.contracts.public import dependency_unavailable


class InternalServiceIdentityProvider(Protocol):
    """Resolve a trusted mTLS/workload identity and its tenant-scoped grants."""

    async def authenticate(self, request: Request) -> EventWriteAccess: ...


def create_event_router(
    identity_provider: InternalServiceIdentityProvider | None,
    service: RunEventIngestionService | None,
) -> APIRouter:
    router = APIRouter(prefix="/internal/v1")

    @router.post(
        "/runs/{run_id}/events:batch",
        tags=["InternalEvents"],
        operation_id="appendRunEventCandidates",
        response_model=RunEventBatchResponse,
    )
    async def append_run_event_candidates(  # pyright: ignore[reportUnusedFunction]
        run_id: str,
        body: RunEventBatchRequest,
        request: Request,
    ) -> RunEventBatchResponse:
        if identity_provider is None or service is None:
            raise dependency_unavailable("Internal Event service is not configured.")
        access = await identity_provider.authenticate(request)
        return await service.append_batch(access, run_id=run_id, request=body)

    return router

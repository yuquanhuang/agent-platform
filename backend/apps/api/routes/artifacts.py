"""Frozen Artifact upload, metadata and completion routes."""

from typing import Annotated

from fastapi import APIRouter, Header, Request, status

from packages.application.public import ArtifactManagementService, RequestMetadata
from packages.contracts.generated.core_models import (
    Artifact,
    ArtifactCompleteRequest,
    ArtifactDownload,
    ArtifactUploadAccepted,
    ArtifactUploadCreateRequest,
    OperationAccepted,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    IdentityProvider,
    dependency_unavailable,
)


def create_artifact_router(
    identity_provider: IdentityProvider | None,
    service: ArtifactManagementService | None,
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
    ) -> tuple[AuthenticatedPrincipal, ArtifactManagementService]:
        if identity_provider is None or service is None:
            raise dependency_unavailable("Artifact service is not configured.")
        return identity_provider.authenticate(authorization), service

    @router.post(
        "/artifacts/uploads",
        tags=["Artifacts"],
        operation_id="createArtifactUpload",
        response_model=ArtifactUploadAccepted,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_artifact_upload(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        body: ArtifactUploadCreateRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> ArtifactUploadAccepted:
        principal, configured_service = authenticate(authorization)
        return await configured_service.create_upload(
            principal,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )

    @router.get(
        "/artifacts/{artifact_id}",
        tags=["Artifacts"],
        operation_id="getArtifact",
        response_model=Artifact,
    )
    async def get_artifact(  # pyright: ignore[reportUnusedFunction]
        artifact_id: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Artifact:
        principal, configured_service = authenticate(authorization)
        return await configured_service.get_artifact(
            principal,
            artifact_id=artifact_id,
            metadata=metadata(request),
        )

    @router.post(
        "/artifacts/{artifact_id}/complete",
        tags=["Artifacts"],
        operation_id="completeArtifactUpload",
        response_model=Artifact,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def complete_artifact_upload(  # pyright: ignore[reportUnusedFunction]
        artifact_id: str,
        request: Request,
        body: ArtifactCompleteRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Artifact:
        principal, configured_service = authenticate(authorization)
        return await configured_service.complete_upload(
            principal,
            artifact_id=artifact_id,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )

    @router.get(
        "/artifacts/{artifact_id}/download",
        tags=["Artifacts"],
        operation_id="createArtifactDownload",
        response_model=ArtifactDownload,
    )
    async def create_artifact_download(  # pyright: ignore[reportUnusedFunction]
        artifact_id: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> ArtifactDownload:
        principal, configured_service = authenticate(authorization)
        return await configured_service.create_download(
            principal,
            artifact_id=artifact_id,
            metadata=metadata(request),
        )

    @router.delete(
        "/artifacts/{artifact_id}",
        tags=["Artifacts"],
        operation_id="deleteArtifact",
        response_model=OperationAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def delete_artifact(  # pyright: ignore[reportUnusedFunction]
        artifact_id: str,
        request: Request,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> OperationAccepted:
        principal, configured_service = authenticate(authorization)
        return await configured_service.delete_artifact(
            principal,
            artifact_id=artifact_id,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )

    return router

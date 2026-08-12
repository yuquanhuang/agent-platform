"""Frozen Artifact upload, metadata and completion routes."""

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Header, Query, Request, status
from starlette.responses import StreamingResponse

from packages.application.public import (
    ArtifactDownloadGatewayService,
    ArtifactManagementService,
    RequestMetadata,
)
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
    download_gateway: ArtifactDownloadGatewayService | None = None,
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

    @router.get(
        "/artifact-downloads/{grant_id}",
        tags=["Artifacts"],
        operation_id="downloadArtifactContent",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "Revocable private Artifact content stream.",
                "content": {
                    "application/octet-stream": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                },
            }
        },
    )
    async def download_artifact_content(  # pyright: ignore[reportUnusedFunction]
        grant_id: str,
        request: Request,
        token: Annotated[
            str,
            Query(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
        ],
        range_header: Annotated[
            str | None,
            Header(alias="Range", max_length=128),
        ] = None,
    ) -> StreamingResponse:
        if download_gateway is None:
            raise dependency_unavailable("Artifact Download Gateway is not configured.")
        content = await download_gateway.open_download(
            grant_id=grant_id,
            token=token,
            range_header=range_header,
            metadata=metadata(request),
        )
        safe_name = _safe_filename(content.name)
        headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, no-store",
            "Content-Disposition": (
                f'attachment; filename="{safe_name}"; '
                f"filename*=UTF-8''{quote(content.name, safe='') }"
            ),
            "Content-Length": str(content.size_bytes),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        }
        response_status = status.HTTP_200_OK
        if content.byte_range is not None:
            response_status = status.HTTP_206_PARTIAL_CONTENT
            headers["Content-Range"] = (
                f"bytes {content.byte_range.start}-"
                f"{content.byte_range.end_inclusive}/{content.total_size_bytes}"
            )
        return StreamingResponse(
            content.body,
            status_code=response_status,
            media_type=content.content_type,
            headers=headers,
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


def _safe_filename(name: str) -> str:
    value = "".join(
        "_" if ord(char) < 32 or char in {'"', "\\"} else char for char in name
    )
    value = value.strip() or "artifact"
    return value.encode("ascii", "replace").decode("ascii")[:255] or "artifact"

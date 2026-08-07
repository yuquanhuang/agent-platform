"""Frozen Agent Release routes."""

from typing import Annotated, cast

from fastapi import APIRouter, Header, Query, Request, status

from packages.application.public import (
    DeploymentManagementService,
    PublicationQueryService,
    ReleaseManagementService,
    RequestMetadata,
)
from packages.contracts.generated.core_models import (
    AgentVersion,
    AgentVersionPage,
    Deployment,
    PublishAgentPreview,
    PublishAgentPreviewRequest,
    PublishAgentRequest,
    Release,
    ReleaseAccepted,
    RollbackAgentRequest,
    SnapshotDiff,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    IdentityProvider,
    dependency_unavailable,
)


def create_release_router(
    identity_provider: IdentityProvider | None,
    service: ReleaseManagementService | None,
    deployment_service: DeploymentManagementService | None = None,
    publication_query_service: PublicationQueryService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    configured_service = cast(ReleaseManagementService, service)
    configured_deployment_service = cast(
        DeploymentManagementService, deployment_service
    )
    configured_publication_query_service = cast(
        PublicationQueryService, publication_query_service
    )

    def metadata(request: Request) -> RequestMetadata:
        trace_id = getattr(request.state, "trace_id", None)
        request_id = getattr(request.state, "request_id", None)
        if not isinstance(trace_id, str) or not isinstance(request_id, str):
            raise TypeError("request correlation context is not installed")
        return RequestMetadata(request_id=request_id, trace_id=trace_id)

    def authenticate(
        authorization: str | None, *, dependency_configured: bool
    ) -> AuthenticatedPrincipal:
        if identity_provider is None or not dependency_configured:
            raise dependency_unavailable("Agent Release service is not configured.")
        return identity_provider.authenticate(authorization)

    @router.post(
        "/agents/{agent_id}/publish",
        tags=["Releases"],
        operation_id="publishAgent",
        response_model=ReleaseAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def publish_agent(  # pyright: ignore[reportUnusedFunction]
        agent_id: str,
        request: Request,
        body: PublishAgentRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> ReleaseAccepted:
        return await configured_service.publish_agent(
            authenticate(authorization, dependency_configured=service is not None),
            agent_id=agent_id,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )

    @router.post(
        "/agents/{agent_id}/publish-preview",
        tags=["Releases"],
        operation_id="previewAgentPublish",
        response_model=PublishAgentPreview,
    )
    async def preview_agent_publish(  # pyright: ignore[reportUnusedFunction]
        agent_id: str,
        request: Request,
        body: PublishAgentPreviewRequest,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> PublishAgentPreview:
        return await configured_publication_query_service.preview_agent_publish(
            authenticate(
                authorization,
                dependency_configured=publication_query_service is not None,
            ),
            agent_id=agent_id,
            request=body,
            metadata=metadata(request),
        )

    @router.get(
        "/releases/{release_id}",
        tags=["Releases"],
        operation_id="getRelease",
        response_model=Release,
    )
    async def get_release(  # pyright: ignore[reportUnusedFunction]
        release_id: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Release:
        return await configured_service.get_release(
            authenticate(authorization, dependency_configured=service is not None),
            release_id=release_id,
            metadata=metadata(request),
        )

    @router.post(
        "/agents/{agent_id}/rollback",
        tags=["Releases"],
        operation_id="rollbackAgent",
        response_model=ReleaseAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def rollback_agent(  # pyright: ignore[reportUnusedFunction]
        agent_id: str,
        request: Request,
        body: RollbackAgentRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> ReleaseAccepted:
        return await configured_service.rollback_agent(
            authenticate(authorization, dependency_configured=service is not None),
            agent_id=agent_id,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )

    @router.get(
        "/agents/{agent_id}/versions",
        tags=["Releases"],
        operation_id="listAgentVersions",
        response_model=AgentVersionPage,
    )
    async def list_agent_versions(  # pyright: ignore[reportUnusedFunction]
        agent_id: str,
        request: Request,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        cursor: str | None = None,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> AgentVersionPage:
        return await configured_publication_query_service.list_agent_versions(
            authenticate(
                authorization,
                dependency_configured=publication_query_service is not None,
            ),
            agent_id=agent_id,
            limit=limit,
            cursor=cursor,
            metadata=metadata(request),
        )

    @router.get(
        "/agents/{agent_id}/versions/{version_id}",
        tags=["Releases"],
        operation_id="getAgentVersion",
        response_model=AgentVersion,
    )
    async def get_agent_version(  # pyright: ignore[reportUnusedFunction]
        agent_id: str,
        version_id: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> AgentVersion:
        return await configured_publication_query_service.get_agent_version(
            authenticate(
                authorization,
                dependency_configured=publication_query_service is not None,
            ),
            agent_id=agent_id,
            version_id=version_id,
            metadata=metadata(request),
        )

    @router.get(
        "/agents/{agent_id}/diff",
        tags=["Releases"],
        operation_id="diffAgentSnapshots",
        response_model=SnapshotDiff,
    )
    async def diff_agent_snapshots(  # pyright: ignore[reportUnusedFunction]
        agent_id: str,
        request: Request,
        from_snapshot_id: str,
        to_snapshot_id: str,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> SnapshotDiff:
        return await configured_publication_query_service.diff_agent_snapshots(
            authenticate(
                authorization,
                dependency_configured=publication_query_service is not None,
            ),
            agent_id=agent_id,
            from_snapshot_id=from_snapshot_id,
            to_snapshot_id=to_snapshot_id,
            metadata=metadata(request),
        )

    @router.get(
        "/deployments/{deployment_id}",
        tags=["Releases"],
        operation_id="getDeployment",
        response_model=Deployment,
    )
    async def get_deployment(  # pyright: ignore[reportUnusedFunction]
        deployment_id: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Deployment:
        return await configured_deployment_service.get_deployment(
            authenticate(
                authorization,
                dependency_configured=deployment_service is not None,
            ),
            deployment_id=deployment_id,
            metadata=metadata(request),
        )

    return router

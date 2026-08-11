"""Frozen MCP resource API routes."""

# pyright: reportUnusedFunction=false

from typing import Annotated

from fastapi import APIRouter, Body, Header, Query, Request, Response, status

from packages.application.public import McpManagementService, RequestMetadata
from packages.contracts.generated.resources_models import (
    ActionRequest,
    McpServerCreateRequest,
    OperationAccepted,
    Resource,
    ResourceDiff,
    ResourcePage,
    ResourcePublishRequest,
    ResourceReferencePage,
    ResourceRollbackRequest,
    ResourceUpdateRequest,
    ResourceVersion,
    ResourceVersionPage,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    IdentityProvider,
    dependency_unavailable,
)


def create_mcp_router(
    identity_provider: IdentityProvider | None,
    service: McpManagementService | None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/mcp-servers", tags=["McpServers"])

    def configured() -> McpManagementService:
        if service is None:
            raise dependency_unavailable("MCP service is not configured.")
        return service

    def metadata(request: Request) -> RequestMetadata:
        trace_id = getattr(request.state, "trace_id", None)
        request_id = getattr(request.state, "request_id", None)
        if not isinstance(trace_id, str) or not isinstance(request_id, str):
            raise TypeError("request correlation context is not installed")
        return RequestMetadata(request_id=request_id, trace_id=trace_id)

    def authenticate(authorization: str | None) -> AuthenticatedPrincipal:
        if identity_provider is None or service is None:
            raise dependency_unavailable("MCP service is not configured.")
        return identity_provider.authenticate(authorization)

    @router.get("", operation_id="listMcpServers", response_model=ResourcePage)
    async def list_servers(
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        cursor: Annotated[str | None, Query()] = None,
        keyword: Annotated[str | None, Query(max_length=100)] = None,
    ) -> ResourcePage:
        return await configured().list_mcp_servers(
            authenticate(authorization),
            limit=limit,
            cursor=cursor,
            keyword=keyword,
            metadata=metadata(request),
        )

    @router.post(
        "",
        operation_id="createMcpServer",
        response_model=Resource,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_server(
        request: Request,
        response: Response,
        body: McpServerCreateRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Resource:
        result, etag = await configured().create_mcp_server(
            authenticate(authorization),
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.get("/{resource_id}", operation_id="getMcpServer", response_model=Resource)
    async def get_server(
        resource_id: str,
        request: Request,
        response: Response,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Resource:
        result, etag = await configured().get_mcp_server(
            authenticate(authorization),
            resource_id=resource_id,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.patch(
        "/{resource_id}", operation_id="updateMcpServer", response_model=Resource
    )
    async def update_server(
        resource_id: str,
        request: Request,
        response: Response,
        body: ResourceUpdateRequest,
        if_match: Annotated[str, Header(alias="If-Match")],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Resource:
        result, etag = await configured().update_mcp_server(
            authenticate(authorization),
            resource_id=resource_id,
            if_match=if_match,
            request=body,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.delete(
        "/{resource_id}",
        operation_id="deleteMcpServer",
        response_model=OperationAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def delete_server(
        resource_id: str,
        request: Request,
        if_match: Annotated[str, Header(alias="If-Match")],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> OperationAccepted:
        return await configured().delete_mcp_server(
            authenticate(authorization),
            resource_id=resource_id,
            if_match=if_match,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )

    @router.post(
        "/{resource_id}/discover",
        operation_id="discoverMcpCapabilities",
        response_model=OperationAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def discover(
        resource_id: str,
        request: Request,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> OperationAccepted:
        return await configured().discover_mcp_capabilities(
            authenticate(authorization),
            resource_id=resource_id,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )

    @router.post(
        "/{resource_id}/publish",
        operation_id="publishMcpServer",
        response_model=ResourceVersion,
        status_code=status.HTTP_201_CREATED,
    )
    async def publish(
        resource_id: str,
        request: Request,
        body: ResourcePublishRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> ResourceVersion:
        return await configured().publish_mcp_server(
            authenticate(authorization),
            resource_id=resource_id,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )

    async def set_enabled(
        resource_id: str,
        request: Request,
        response: Response,
        if_match: str,
        idempotency_key: str,
        authorization: str | None,
        body: ActionRequest | None,
        *,
        enabled: bool,
    ) -> Resource:
        result, etag = await configured().set_mcp_server_enabled(
            authenticate(authorization),
            resource_id=resource_id,
            if_match=if_match,
            enabled=enabled,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.post(
        "/{resource_id}/disable",
        operation_id="disableMcpServer",
        response_model=Resource,
    )
    async def disable(
        resource_id: str,
        request: Request,
        response: Response,
        if_match: Annotated[str, Header(alias="If-Match")],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        body: Annotated[ActionRequest | None, Body()] = None,
    ) -> Resource:
        return await set_enabled(
            resource_id,
            request,
            response,
            if_match,
            idempotency_key,
            authorization,
            body,
            enabled=False,
        )

    @router.post(
        "/{resource_id}/enable",
        operation_id="enableMcpServer",
        response_model=Resource,
    )
    async def enable(
        resource_id: str,
        request: Request,
        response: Response,
        if_match: Annotated[str, Header(alias="If-Match")],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        body: Annotated[ActionRequest | None, Body()] = None,
    ) -> Resource:
        return await set_enabled(
            resource_id,
            request,
            response,
            if_match,
            idempotency_key,
            authorization,
            body,
            enabled=True,
        )

    @router.post(
        "/{resource_id}/rollback",
        operation_id="rollbackMcpServer",
        response_model=ResourceVersion,
        status_code=status.HTTP_201_CREATED,
    )
    async def rollback(
        resource_id: str,
        request: Request,
        body: ResourceRollbackRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> ResourceVersion:
        return await configured().rollback_mcp_server(
            authenticate(authorization),
            resource_id=resource_id,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )

    @router.get(
        "/{resource_id}/versions",
        operation_id="listMcpServerVersions",
        response_model=ResourceVersionPage,
    )
    async def versions(
        resource_id: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        cursor: Annotated[str | None, Query()] = None,
    ) -> ResourceVersionPage:
        return await configured().list_mcp_server_versions(
            authenticate(authorization),
            resource_id=resource_id,
            limit=limit,
            cursor=cursor,
            metadata=metadata(request),
        )

    @router.get(
        "/{resource_id}/diff",
        operation_id="diffMcpServerVersions",
        response_model=ResourceDiff,
    )
    async def diff(
        resource_id: str,
        request: Request,
        from_version_id: Annotated[str, Query(alias="from_version_id")],
        to_version_id: Annotated[str, Query(alias="to_version_id")],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> ResourceDiff:
        return await configured().diff_mcp_server_versions(
            authenticate(authorization),
            resource_id=resource_id,
            from_version_id=from_version_id,
            to_version_id=to_version_id,
            metadata=metadata(request),
        )

    @router.get(
        "/{resource_id}/references",
        operation_id="listMcpServerReferences",
        response_model=ResourceReferencePage,
    )
    async def references(
        resource_id: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> ResourceReferencePage:
        return await configured().list_mcp_server_references(
            authenticate(authorization),
            resource_id=resource_id,
            metadata=metadata(request),
        )

    return router

"""Frozen Model Provider and Model Config resource routes."""

from typing import Annotated, cast

from fastapi import APIRouter, Body, Header, Query, Request, Response, status

from packages.application.public import ModelManagementService, RequestMetadata
from packages.contracts.generated.resources_models import (
    ActionRequest,
    ModelConfigCreateRequest,
    ModelProviderCreateRequest,
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


def create_model_router(
    identity_provider: IdentityProvider | None,
    service: ModelManagementService | None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    configured_service = cast(ModelManagementService, service)

    def metadata(request: Request) -> RequestMetadata:
        trace_id = getattr(request.state, "trace_id", None)
        request_id = getattr(request.state, "request_id", None)
        if not isinstance(trace_id, str) or not isinstance(request_id, str):
            raise TypeError("request correlation context is not installed")
        return RequestMetadata(request_id=request_id, trace_id=trace_id)

    def authenticate(authorization: str | None) -> AuthenticatedPrincipal:
        if identity_provider is None or service is None:
            raise dependency_unavailable("Model resource service is not configured.")
        return identity_provider.authenticate(authorization)

    @router.get(
        "/model-providers",
        tags=["ModelProviders"],
        operation_id="listModelProviders",
        response_model=ResourcePage,
    )
    async def list_model_providers(
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        cursor: Annotated[str | None, Query()] = None,
    ) -> ResourcePage:
        return await configured_service.list_model_providers(
            authenticate(authorization),
            limit=limit,
            cursor=cursor,
            metadata=metadata(request),
        )

    @router.post(
        "/model-providers",
        tags=["ModelProviders"],
        operation_id="createModelProvider",
        response_model=Resource,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_model_provider(
        request: Request,
        response: Response,
        body: ModelProviderCreateRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Resource:
        result, etag = await configured_service.create_model_provider(
            authenticate(authorization),
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.get(
        "/model-providers/{resource_id}",
        tags=["ModelProviders"],
        operation_id="getModelProvider",
        response_model=Resource,
    )
    async def get_model_provider(
        resource_id: str,
        request: Request,
        response: Response,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Resource:
        result, etag = await configured_service.get_model_provider(
            authenticate(authorization),
            resource_id=resource_id,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.patch(
        "/model-providers/{resource_id}",
        tags=["ModelProviders"],
        operation_id="updateModelProvider",
        response_model=Resource,
    )
    async def update_model_provider(
        resource_id: str,
        request: Request,
        response: Response,
        body: ResourceUpdateRequest,
        if_match: Annotated[str, Header(alias="If-Match")],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Resource:
        result, etag = await configured_service.update_model_provider(
            authenticate(authorization),
            resource_id=resource_id,
            if_match=if_match,
            request=body,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.delete(
        "/model-providers/{resource_id}",
        tags=["ModelProviders"],
        operation_id="deleteModelProvider",
        response_model=OperationAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def delete_model_provider(
        resource_id: str,
        request: Request,
        if_match: Annotated[str, Header(alias="If-Match")],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> OperationAccepted:
        return await configured_service.delete_model_provider(
            authenticate(authorization),
            resource_id=resource_id,
            if_match=if_match,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )

    @router.post(
        "/model-providers/{resource_id}/test",
        tags=["ModelProviders"],
        operation_id="testModelProviderConnection",
        response_model=OperationAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def test_model_provider_connection(
        resource_id: str,
        request: Request,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> OperationAccepted:
        return await configured_service.test_model_provider_connection(
            authenticate(authorization),
            resource_id=resource_id,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )

    async def set_provider_enabled(
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
        result, etag = await configured_service.set_model_provider_enabled(
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
        "/model-providers/{resource_id}/disable",
        tags=["ModelProviders"],
        operation_id="disableModelProvider",
        response_model=Resource,
    )
    async def disable_model_provider(
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
        return await set_provider_enabled(
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
        "/model-providers/{resource_id}/enable",
        tags=["ModelProviders"],
        operation_id="enableModelProvider",
        response_model=Resource,
    )
    async def enable_model_provider(
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
        return await set_provider_enabled(
            resource_id,
            request,
            response,
            if_match,
            idempotency_key,
            authorization,
            body,
            enabled=True,
        )

    @router.get(
        "/model-configs",
        tags=["ModelConfigs"],
        operation_id="listModelConfigs",
        response_model=ResourcePage,
    )
    async def list_model_configs(
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        cursor: Annotated[str | None, Query()] = None,
    ) -> ResourcePage:
        return await configured_service.list_model_configs(
            authenticate(authorization),
            limit=limit,
            cursor=cursor,
            metadata=metadata(request),
        )

    @router.post(
        "/model-configs",
        tags=["ModelConfigs"],
        operation_id="createModelConfig",
        response_model=Resource,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_model_config(
        request: Request,
        response: Response,
        body: ModelConfigCreateRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Resource:
        result, etag = await configured_service.create_model_config(
            authenticate(authorization),
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.get(
        "/model-configs/{resource_id}",
        tags=["ModelConfigs"],
        operation_id="getModelConfig",
        response_model=Resource,
    )
    async def get_model_config(
        resource_id: str,
        request: Request,
        response: Response,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Resource:
        result, etag = await configured_service.get_model_config(
            authenticate(authorization),
            resource_id=resource_id,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.patch(
        "/model-configs/{resource_id}",
        tags=["ModelConfigs"],
        operation_id="updateModelConfig",
        response_model=Resource,
    )
    async def update_model_config(
        resource_id: str,
        request: Request,
        response: Response,
        body: ResourceUpdateRequest,
        if_match: Annotated[str, Header(alias="If-Match")],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Resource:
        result, etag = await configured_service.update_model_config(
            authenticate(authorization),
            resource_id=resource_id,
            if_match=if_match,
            request=body,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.delete(
        "/model-configs/{resource_id}",
        tags=["ModelConfigs"],
        operation_id="deleteModelConfig",
        response_model=OperationAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def delete_model_config(
        resource_id: str,
        request: Request,
        if_match: Annotated[str, Header(alias="If-Match")],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> OperationAccepted:
        return await configured_service.delete_model_config(
            authenticate(authorization),
            resource_id=resource_id,
            if_match=if_match,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )

    @router.post(
        "/model-configs/{resource_id}/publish",
        tags=["ModelConfigs"],
        operation_id="publishModelConfig",
        response_model=ResourceVersion,
        status_code=status.HTTP_201_CREATED,
    )
    async def publish_model_config(
        resource_id: str,
        request: Request,
        body: ResourcePublishRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> ResourceVersion:
        return await configured_service.publish_model_config(
            authenticate(authorization),
            resource_id=resource_id,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )

    async def set_config_enabled(
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
        result, etag = await configured_service.set_model_config_enabled(
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
        "/model-configs/{resource_id}/disable",
        tags=["ModelConfigs"],
        operation_id="disableModelConfig",
        response_model=Resource,
    )
    async def disable_model_config(
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
        return await set_config_enabled(
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
        "/model-configs/{resource_id}/enable",
        tags=["ModelConfigs"],
        operation_id="enableModelConfig",
        response_model=Resource,
    )
    async def enable_model_config(
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
        return await set_config_enabled(
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
        "/model-configs/{resource_id}/rollback",
        tags=["ModelConfigs"],
        operation_id="rollbackModelConfig",
        response_model=ResourceVersion,
        status_code=status.HTTP_201_CREATED,
    )
    async def rollback_model_config(
        resource_id: str,
        request: Request,
        body: ResourceRollbackRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> ResourceVersion:
        return await configured_service.rollback_model_config(
            authenticate(authorization),
            resource_id=resource_id,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )

    @router.get(
        "/model-configs/{resource_id}/versions",
        tags=["ModelConfigs"],
        operation_id="listModelConfigVersions",
        response_model=ResourceVersionPage,
    )
    async def list_model_config_versions(
        resource_id: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        cursor: Annotated[str | None, Query()] = None,
    ) -> ResourceVersionPage:
        return await configured_service.list_model_config_versions(
            authenticate(authorization),
            resource_id=resource_id,
            limit=limit,
            cursor=cursor,
            metadata=metadata(request),
        )

    @router.get(
        "/model-configs/{resource_id}/diff",
        tags=["ModelConfigs"],
        operation_id="diffModelConfigVersions",
        response_model=ResourceDiff,
    )
    async def diff_model_config_versions(
        resource_id: str,
        request: Request,
        from_version_id: Annotated[str, Query(alias="from_version_id")],
        to_version_id: Annotated[str, Query(alias="to_version_id")],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> ResourceDiff:
        return await configured_service.diff_model_config_versions(
            authenticate(authorization),
            resource_id=resource_id,
            from_version_id=from_version_id,
            to_version_id=to_version_id,
            metadata=metadata(request),
        )

    @router.get(
        "/model-configs/{resource_id}/references",
        tags=["ModelConfigs"],
        operation_id="listModelConfigReferences",
        response_model=ResourceReferencePage,
    )
    async def list_model_config_references(
        resource_id: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> ResourceReferencePage:
        return await configured_service.list_model_config_references(
            authenticate(authorization),
            resource_id=resource_id,
            metadata=metadata(request),
        )

    _registered_routes = (
        list_model_providers,
        create_model_provider,
        get_model_provider,
        update_model_provider,
        delete_model_provider,
        test_model_provider_connection,
        disable_model_provider,
        enable_model_provider,
        list_model_configs,
        create_model_config,
        get_model_config,
        update_model_config,
        delete_model_config,
        publish_model_config,
        disable_model_config,
        enable_model_config,
        rollback_model_config,
        list_model_config_versions,
        diff_model_config_versions,
        list_model_config_references,
    )

    return router

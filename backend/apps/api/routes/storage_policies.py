"""Tenant storage capacity policy administration routes."""

# pyright: reportUnusedFunction=false

from typing import Annotated, cast

from fastapi import APIRouter, Header, Query, Request, Response, status

from packages.application.public import RequestMetadata, StoragePolicyManagementService
from packages.contracts.generated.resources_models import (
    ActionRequest,
    StoragePolicy,
    StoragePolicyCreateRequest,
    StoragePolicyPage,
    StoragePolicyUpdateRequest,
    StoragePolicyVersionPage,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    IdentityProvider,
    dependency_unavailable,
)


def create_storage_policy_router(
    identity_provider: IdentityProvider | None,
    service: StoragePolicyManagementService | None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["StoragePolicies"])
    configured_service = cast(StoragePolicyManagementService, service)

    def metadata(request: Request) -> RequestMetadata:
        trace_id = getattr(request.state, "trace_id", None)
        request_id = getattr(request.state, "request_id", None)
        if not isinstance(trace_id, str) or not isinstance(request_id, str):
            raise TypeError("request correlation context is not installed")
        return RequestMetadata(request_id=request_id, trace_id=trace_id)

    def authenticate(authorization: str | None) -> AuthenticatedPrincipal:
        if identity_provider is None or service is None:
            raise dependency_unavailable("StoragePolicy service is not configured.")
        return identity_provider.authenticate(authorization)

    @router.get(
        "/storage-policies",
        operation_id="listStoragePolicies",
        response_model=StoragePolicyPage,
    )
    async def list_storage_policies(
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        cursor: Annotated[str | None, Query()] = None,
    ) -> StoragePolicyPage:
        principal = authenticate(authorization)
        return await configured_service.list_policies(
            principal,
            limit=limit,
            cursor=cursor,
            metadata=metadata(request),
        )

    @router.post(
        "/storage-policies",
        operation_id="createStoragePolicy",
        response_model=StoragePolicy,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_storage_policy(
        request: Request,
        response: Response,
        body: StoragePolicyCreateRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> StoragePolicy:
        principal = authenticate(authorization)
        result, etag = await configured_service.create_policy(
            principal,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.get(
        "/storage-policies/{storage_policy_id}",
        operation_id="getStoragePolicy",
        response_model=StoragePolicy,
    )
    async def get_storage_policy(
        request: Request,
        storage_policy_id: str,
        response: Response,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> StoragePolicy:
        principal = authenticate(authorization)
        result, etag = await configured_service.get_policy(
            principal,
            policy_id=storage_policy_id,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.patch(
        "/storage-policies/{storage_policy_id}",
        operation_id="updateStoragePolicy",
        response_model=StoragePolicy,
    )
    async def update_storage_policy(
        request: Request,
        storage_policy_id: str,
        response: Response,
        body: StoragePolicyUpdateRequest,
        if_match: Annotated[
            str, Header(alias="If-Match", pattern=r'^"rv:[1-9][0-9]*"$')
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> StoragePolicy:
        principal = authenticate(authorization)
        result, etag = await configured_service.update_policy(
            principal,
            policy_id=storage_policy_id,
            if_match=if_match,
            request=body,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.post(
        "/storage-policies/{storage_policy_id}/disable",
        operation_id="disableStoragePolicy",
        response_model=StoragePolicy,
    )
    async def disable_storage_policy(
        request: Request,
        storage_policy_id: str,
        response: Response,
        if_match: Annotated[
            str, Header(alias="If-Match", pattern=r'^"rv:[1-9][0-9]*"$')
        ],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        body: ActionRequest | None = None,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> StoragePolicy:
        principal = authenticate(authorization)
        result, etag = await configured_service.set_policy_enabled(
            principal,
            policy_id=storage_policy_id,
            if_match=if_match,
            enabled=False,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.post(
        "/storage-policies/{storage_policy_id}/enable",
        operation_id="enableStoragePolicy",
        response_model=StoragePolicy,
    )
    async def enable_storage_policy(
        request: Request,
        storage_policy_id: str,
        response: Response,
        if_match: Annotated[
            str, Header(alias="If-Match", pattern=r'^"rv:[1-9][0-9]*"$')
        ],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        body: ActionRequest | None = None,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> StoragePolicy:
        principal = authenticate(authorization)
        result, etag = await configured_service.set_policy_enabled(
            principal,
            policy_id=storage_policy_id,
            if_match=if_match,
            enabled=True,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.get(
        "/storage-policies/{storage_policy_id}/versions",
        operation_id="listStoragePolicyVersions",
        response_model=StoragePolicyVersionPage,
    )
    async def list_storage_policy_versions(
        request: Request,
        storage_policy_id: str,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        cursor: Annotated[str | None, Query()] = None,
    ) -> StoragePolicyVersionPage:
        principal = authenticate(authorization)
        return await configured_service.list_versions(
            principal,
            policy_id=storage_policy_id,
            limit=limit,
            cursor=cursor,
            metadata=metadata(request),
        )

    return router

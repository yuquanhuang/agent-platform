"""Frozen Skill resource API routes."""

from typing import Annotated

from fastapi import APIRouter, Body, Header, Query, Request, Response, status

from packages.application.public import RequestMetadata, SkillManagementService
from packages.contracts.generated.resources_models import (
    ActionRequest,
    OperationAccepted,
    Resource,
    ResourceCopyRequest,
    ResourceDiff,
    ResourcePage,
    ResourcePublishRequest,
    ResourceReferencePage,
    ResourceRollbackRequest,
    ResourceUpdateRequest,
    ResourceVersion,
    ResourceVersionPage,
    SkillCreateRequest,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    IdentityProvider,
    dependency_unavailable,
)


def create_skill_router(
    identity_provider: IdentityProvider | None,
    service: SkillManagementService | None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/skills", tags=["Skills"])

    def configured_service() -> SkillManagementService:
        if service is None:
            raise dependency_unavailable("Skill service is not configured.")
        return service

    def metadata(request: Request) -> RequestMetadata:
        trace_id = getattr(request.state, "trace_id", None)
        request_id = getattr(request.state, "request_id", None)
        if not isinstance(trace_id, str) or not isinstance(request_id, str):
            raise TypeError("request correlation context is not installed")
        return RequestMetadata(request_id=request_id, trace_id=trace_id)

    def authenticate(authorization: str | None) -> AuthenticatedPrincipal:
        if identity_provider is None or service is None:
            raise dependency_unavailable("Skill service is not configured.")
        return identity_provider.authenticate(authorization)

    @router.get("", operation_id="listSkills", response_model=ResourcePage)
    async def list_skills(
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        cursor: Annotated[str | None, Query()] = None,
        keyword: Annotated[str | None, Query(max_length=100)] = None,
    ) -> ResourcePage:
        return await configured_service().list_skills(
            authenticate(authorization),
            limit=limit,
            cursor=cursor,
            keyword=keyword,
            metadata=metadata(request),
        )

    @router.post(
        "",
        operation_id="createSkill",
        response_model=Resource,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_skill(
        request: Request,
        response: Response,
        body: SkillCreateRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Resource:
        result, etag = await configured_service().create_skill(
            authenticate(authorization),
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.get("/{resource_id}", operation_id="getSkill", response_model=Resource)
    async def get_skill(
        resource_id: str,
        request: Request,
        response: Response,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Resource:
        result, etag = await configured_service().get_skill(
            authenticate(authorization),
            resource_id=resource_id,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.patch("/{resource_id}", operation_id="updateSkill", response_model=Resource)
    async def update_skill(
        resource_id: str,
        request: Request,
        response: Response,
        body: ResourceUpdateRequest,
        if_match: Annotated[str, Header(alias="If-Match")],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Resource:
        result, etag = await configured_service().update_skill(
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
        operation_id="deleteSkill",
        response_model=OperationAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def delete_skill(
        resource_id: str,
        request: Request,
        if_match: Annotated[str, Header(alias="If-Match")],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> OperationAccepted:
        return await configured_service().delete_skill(
            authenticate(authorization),
            resource_id=resource_id,
            if_match=if_match,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )

    @router.post(
        "/{resource_id}/publish",
        operation_id="publishSkill",
        response_model=ResourceVersion,
        status_code=status.HTTP_201_CREATED,
    )
    async def publish_skill(
        resource_id: str,
        request: Request,
        body: ResourcePublishRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> ResourceVersion:
        return await configured_service().publish_skill(
            authenticate(authorization),
            resource_id=resource_id,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )

    @router.post(
        "/{resource_id}/copy",
        operation_id="copySkill",
        response_model=Resource,
        status_code=status.HTTP_201_CREATED,
    )
    async def copy_skill(
        resource_id: str,
        request: Request,
        response: Response,
        body: ResourceCopyRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Resource:
        result, etag = await configured_service().copy_skill(
            authenticate(authorization),
            resource_id=resource_id,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

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
        result, etag = await configured_service().set_skill_enabled(
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
        operation_id="disableSkill",
        response_model=Resource,
    )
    async def disable_skill(
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
        operation_id="enableSkill",
        response_model=Resource,
    )
    async def enable_skill(
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
        operation_id="rollbackSkill",
        response_model=ResourceVersion,
        status_code=status.HTTP_201_CREATED,
    )
    async def rollback_skill(
        resource_id: str,
        request: Request,
        body: ResourceRollbackRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> ResourceVersion:
        return await configured_service().rollback_skill(
            authenticate(authorization),
            resource_id=resource_id,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )

    @router.get(
        "/{resource_id}/versions",
        operation_id="listSkillVersions",
        response_model=ResourceVersionPage,
    )
    async def list_skill_versions(
        resource_id: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        cursor: Annotated[str | None, Query()] = None,
    ) -> ResourceVersionPage:
        return await configured_service().list_skill_versions(
            authenticate(authorization),
            resource_id=resource_id,
            limit=limit,
            cursor=cursor,
            metadata=metadata(request),
        )

    @router.get(
        "/{resource_id}/diff",
        operation_id="diffSkillVersions",
        response_model=ResourceDiff,
    )
    async def diff_skill_versions(
        resource_id: str,
        request: Request,
        from_version_id: Annotated[str, Query(alias="from_version_id")],
        to_version_id: Annotated[str, Query(alias="to_version_id")],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> ResourceDiff:
        return await configured_service().diff_skill_versions(
            authenticate(authorization),
            resource_id=resource_id,
            from_version_id=from_version_id,
            to_version_id=to_version_id,
            metadata=metadata(request),
        )

    @router.get(
        "/{resource_id}/references",
        operation_id="listSkillReferences",
        response_model=ResourceReferencePage,
    )
    async def list_skill_references(
        resource_id: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> ResourceReferencePage:
        return await configured_service().list_skill_references(
            authenticate(authorization),
            resource_id=resource_id,
            metadata=metadata(request),
        )

    _registered_routes = (
        list_skills,
        create_skill,
        get_skill,
        update_skill,
        delete_skill,
        publish_skill,
        copy_skill,
        disable_skill,
        enable_skill,
        rollback_skill,
        list_skill_versions,
        diff_skill_versions,
        list_skill_references,
    )

    return router

"""Tenant, member, role and operation API routes."""

from typing import Annotated, cast

from fastapi import APIRouter, Header, Query, Request, Response, status

from packages.application.public import IamManagementService, RequestMetadata
from packages.contracts.generated.core_models import Operation
from packages.contracts.generated.resources_models import (
    ActionRequest,
    Member,
    MemberCreateRequest,
    MemberPage,
    MemberUpdateRequest,
    OperationAccepted,
    Role,
    RoleCreateRequest,
    RolePage,
    RoleUpdateRequest,
    Tenant,
    TenantCreateRequest,
    TenantPage,
    TenantUpdateRequest,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    IdentityProvider,
    dependency_unavailable,
)


def create_iam_router(
    identity_provider: IdentityProvider | None,
    service: IamManagementService | None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["IAM"])
    configured_service = cast(IamManagementService, service)

    def metadata(request: Request) -> RequestMetadata:
        trace_id = getattr(request.state, "trace_id", None)
        if not isinstance(trace_id, str):
            raise TypeError("request trace context is not installed")
        request_id = getattr(request.state, "request_id", None)
        if not isinstance(request_id, str):
            raise TypeError("request correlation context is not installed")
        return RequestMetadata(request_id=request_id, trace_id=trace_id)

    def authenticate(authorization: str | None) -> AuthenticatedPrincipal:
        if identity_provider is None or service is None:
            raise dependency_unavailable("IAM service is not configured.")
        return identity_provider.authenticate(authorization)

    @router.get("/tenants", operation_id="listTenants", response_model=TenantPage)
    async def list_tenants(
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        cursor: Annotated[str | None, Query()] = None,
    ) -> TenantPage:
        principal = authenticate(authorization)
        return await configured_service.list_tenants(
            principal, limit=limit, cursor=cursor
        )

    @router.post(
        "/tenants",
        operation_id="createTenant",
        response_model=Tenant,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_tenant(
        request: Request,
        response: Response,
        body: TenantCreateRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Tenant:
        principal = authenticate(authorization)
        result, etag = await configured_service.create_tenant(
            principal,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.get("/tenants/{tenant_id}", operation_id="getTenant", response_model=Tenant)
    async def get_tenant(
        tenant_id: str,
        response: Response,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Tenant:
        principal = authenticate(authorization)
        result, etag = await configured_service.get_tenant(principal, tenant_id)
        response.headers["ETag"] = etag
        return result

    @router.patch(
        "/tenants/{tenant_id}", operation_id="updateTenant", response_model=Tenant
    )
    async def update_tenant(
        request: Request,
        tenant_id: str,
        response: Response,
        body: TenantUpdateRequest,
        if_match: Annotated[
            str, Header(alias="If-Match", pattern=r'^"rv:[1-9][0-9]*"$')
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Tenant:
        principal = authenticate(authorization)
        result, etag = await configured_service.update_tenant(
            principal,
            tenant_id=tenant_id,
            if_match=if_match,
            request=body,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.post(
        "/tenants/{tenant_id}/disable",
        operation_id="disableTenant",
        response_model=Tenant,
    )
    async def disable_tenant(
        request: Request,
        tenant_id: str,
        response: Response,
        if_match: Annotated[
            str, Header(alias="If-Match", pattern=r'^"rv:[1-9][0-9]*"$')
        ],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        body: ActionRequest | None = None,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Tenant:
        principal = authenticate(authorization)
        result, etag = await configured_service.set_tenant_status(
            principal,
            tenant_id=tenant_id,
            if_match=if_match,
            status="DISABLED",
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.post(
        "/tenants/{tenant_id}/enable",
        operation_id="enableTenant",
        response_model=Tenant,
    )
    async def enable_tenant(
        request: Request,
        tenant_id: str,
        response: Response,
        if_match: Annotated[
            str, Header(alias="If-Match", pattern=r'^"rv:[1-9][0-9]*"$')
        ],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        body: ActionRequest | None = None,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Tenant:
        principal = authenticate(authorization)
        result, etag = await configured_service.set_tenant_status(
            principal,
            tenant_id=tenant_id,
            if_match=if_match,
            status="ACTIVE",
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.get("/members", operation_id="listMembers", response_model=MemberPage)
    async def list_members(
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        cursor: Annotated[str | None, Query()] = None,
    ) -> MemberPage:
        principal = authenticate(authorization)
        return await configured_service.list_members(
            principal, limit=limit, cursor=cursor, metadata=metadata(request)
        )

    @router.post(
        "/members",
        operation_id="createMember",
        response_model=Member,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_member(
        request: Request,
        response: Response,
        body: MemberCreateRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Member:
        principal = authenticate(authorization)
        result, etag = await configured_service.create_member(
            principal,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.get("/members/{member_id}", operation_id="getMember", response_model=Member)
    async def get_member(
        request: Request,
        member_id: str,
        response: Response,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Member:
        principal = authenticate(authorization)
        result, etag = await configured_service.get_member(
            principal, member_id, metadata(request)
        )
        response.headers["ETag"] = etag
        return result

    @router.patch(
        "/members/{member_id}", operation_id="updateMember", response_model=Member
    )
    async def update_member(
        request: Request,
        member_id: str,
        response: Response,
        body: MemberUpdateRequest,
        if_match: Annotated[
            str, Header(alias="If-Match", pattern=r'^"rv:[1-9][0-9]*"$')
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Member:
        principal = authenticate(authorization)
        result, etag = await configured_service.update_member(
            principal,
            member_id=member_id,
            if_match=if_match,
            request=body,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.delete(
        "/members/{member_id}",
        operation_id="deleteMember",
        response_model=OperationAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def delete_member(
        request: Request,
        member_id: str,
        if_match: Annotated[
            str, Header(alias="If-Match", pattern=r'^"rv:[1-9][0-9]*"$')
        ],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> OperationAccepted:
        principal = authenticate(authorization)
        return await configured_service.delete_member(
            principal,
            member_id=member_id,
            if_match=if_match,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )

    @router.get("/roles", operation_id="listRoles", response_model=RolePage)
    async def list_roles(
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        cursor: Annotated[str | None, Query()] = None,
    ) -> RolePage:
        principal = authenticate(authorization)
        return await configured_service.list_roles(
            principal, limit=limit, cursor=cursor, metadata=metadata(request)
        )

    @router.post(
        "/roles",
        operation_id="createRole",
        response_model=Role,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_role(
        request: Request,
        response: Response,
        body: RoleCreateRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Role:
        principal = authenticate(authorization)
        result, etag = await configured_service.create_role(
            principal,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.get("/roles/{role_id}", operation_id="getRole", response_model=Role)
    async def get_role(
        request: Request,
        role_id: str,
        response: Response,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Role:
        principal = authenticate(authorization)
        result, etag = await configured_service.get_role(
            principal, role_id, metadata(request)
        )
        response.headers["ETag"] = etag
        return result

    @router.patch("/roles/{role_id}", operation_id="updateRole", response_model=Role)
    async def update_role(
        request: Request,
        role_id: str,
        response: Response,
        body: RoleUpdateRequest,
        if_match: Annotated[
            str, Header(alias="If-Match", pattern=r'^"rv:[1-9][0-9]*"$')
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Role:
        principal = authenticate(authorization)
        result, etag = await configured_service.update_role(
            principal,
            role_id=role_id,
            if_match=if_match,
            request=body,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.delete(
        "/roles/{role_id}",
        operation_id="deleteRole",
        response_model=OperationAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def delete_role(
        request: Request,
        role_id: str,
        if_match: Annotated[
            str, Header(alias="If-Match", pattern=r'^"rv:[1-9][0-9]*"$')
        ],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> OperationAccepted:
        principal = authenticate(authorization)
        return await configured_service.delete_role(
            principal,
            role_id=role_id,
            if_match=if_match,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )

    @router.get(
        "/operations/{operation_id}",
        operation_id="getOperation",
        response_model=Operation,
    )
    async def get_operation(
        request: Request,
        operation_id: str,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Operation:
        principal = authenticate(authorization)
        return await configured_service.get_operation(
            principal, operation_id, metadata(request)
        )

    _registered_handlers = (
        list_tenants,
        create_tenant,
        get_tenant,
        update_tenant,
        disable_tenant,
        enable_tenant,
        list_members,
        create_member,
        get_member,
        update_member,
        delete_member,
        list_roles,
        create_role,
        get_role,
        update_role,
        delete_role,
        get_operation,
    )

    return router

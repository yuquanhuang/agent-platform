"""Tenant, member, role and operation application services."""

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel

from packages.application.metadata import RequestMetadata
from packages.application.resources import canonical_request_hash
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
    permission_denied,
    resource_not_found,
    unauthenticated,
    validation_error,
)
from packages.domain.public import (
    MemberRecord,
    MutationOutcome,
    OperationRecord,
    PermissionSet,
    RoleRecord,
    TenantAccess,
    TenantRecord,
    format_etag,
    parse_etag,
)


class IamPersistence(Protocol):
    async def resolve_platform_actor(
        self, principal: AuthenticatedPrincipal
    ) -> UUID | None: ...

    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess: ...

    async def list_tenants(
        self, *, limit: int, cursor: str | None
    ) -> tuple[list[TenantRecord], str | None]: ...

    async def create_tenant(
        self,
        *,
        actor_id: UUID,
        request: TenantCreateRequest,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[TenantRecord]: ...

    async def get_tenant(self, tenant_id: UUID) -> TenantRecord | None: ...

    async def update_tenant(
        self,
        *,
        actor_id: UUID,
        tenant_id: UUID,
        expected_version: int,
        request: TenantUpdateRequest,
        metadata: RequestMetadata,
    ) -> TenantRecord | None: ...

    async def set_tenant_status(
        self,
        *,
        actor_id: UUID,
        tenant_id: UUID,
        expected_version: int,
        status: str,
        request: ActionRequest | None,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[TenantRecord]: ...

    async def list_members(
        self, access: TenantAccess, *, limit: int, cursor: str | None
    ) -> tuple[list[MemberRecord], str | None]: ...

    async def create_member(
        self,
        access: TenantAccess,
        *,
        issuer: str,
        request: MemberCreateRequest,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[MemberRecord]: ...

    async def get_member(
        self, access: TenantAccess, member_id: UUID
    ) -> MemberRecord | None: ...

    async def update_member(
        self,
        access: TenantAccess,
        *,
        member_id: UUID,
        expected_version: int,
        request: MemberUpdateRequest,
        metadata: RequestMetadata,
    ) -> MemberRecord | None: ...

    async def delete_member(
        self,
        access: TenantAccess,
        *,
        member_id: UUID,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[OperationRecord]: ...

    async def list_roles(
        self, access: TenantAccess, *, limit: int, cursor: str | None
    ) -> tuple[list[RoleRecord], str | None]: ...

    async def create_role(
        self,
        access: TenantAccess,
        *,
        request: RoleCreateRequest,
        permissions: tuple[str, ...],
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[RoleRecord]: ...

    async def get_role(
        self, access: TenantAccess, role_id: UUID
    ) -> RoleRecord | None: ...

    async def update_role(
        self,
        access: TenantAccess,
        *,
        role_id: UUID,
        expected_version: int,
        request: RoleUpdateRequest,
        permissions: tuple[str, ...] | None,
        metadata: RequestMetadata,
    ) -> RoleRecord | None: ...

    async def delete_role(
        self,
        access: TenantAccess,
        *,
        role_id: UUID,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[OperationRecord]: ...

    async def get_operation(
        self, access: TenantAccess, operation_id: UUID
    ) -> OperationRecord | None: ...


class IamManagementService:
    """Authorize IAM use cases and map persistence records to frozen DTOs."""

    def __init__(self, persistence: IamPersistence) -> None:
        self._persistence = persistence

    async def list_tenants(
        self, principal: AuthenticatedPrincipal, *, limit: int, cursor: str | None
    ) -> TenantPage:
        await self._platform_actor(principal)
        records, next_cursor = await self._persistence.list_tenants(
            limit=limit, cursor=cursor
        )
        return TenantPage(
            items=[_tenant(record) for record in records],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    async def create_tenant(
        self,
        principal: AuthenticatedPrincipal,
        *,
        request: TenantCreateRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> tuple[Tenant, str]:
        actor_id = await self._platform_actor(principal)
        outcome = await self._persistence.create_tenant(
            actor_id=actor_id,
            request=request,
            idempotency_key=idempotency_key,
            request_hash=_request_hash("tenant.create", request),
            metadata=metadata,
        )
        return _versioned_outcome(outcome, Tenant, _tenant)

    async def get_tenant(
        self, principal: AuthenticatedPrincipal, tenant_id: str
    ) -> tuple[Tenant, str]:
        await self._platform_actor(principal)
        record = await self._persistence.get_tenant(_resource_id(tenant_id))
        if record is None:
            raise resource_not_found()
        return _tenant(record), format_etag(record.resource_version)

    async def update_tenant(
        self,
        principal: AuthenticatedPrincipal,
        *,
        tenant_id: str,
        if_match: str,
        request: TenantUpdateRequest,
        metadata: RequestMetadata,
    ) -> tuple[Tenant, str]:
        if not request.model_fields_set:
            raise validation_error("Tenant update requires at least one field.")
        actor_id = await self._platform_actor(principal)
        record = await self._persistence.update_tenant(
            actor_id=actor_id,
            tenant_id=_resource_id(tenant_id),
            expected_version=parse_etag(if_match),
            request=request,
            metadata=metadata,
        )
        if record is None:
            raise resource_not_found()
        return _tenant(record), format_etag(record.resource_version)

    async def set_tenant_status(
        self,
        principal: AuthenticatedPrincipal,
        *,
        tenant_id: str,
        if_match: str,
        status: str,
        request: ActionRequest | None,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> tuple[Tenant, str]:
        actor_id = await self._platform_actor(principal)
        outcome = await self._persistence.set_tenant_status(
            actor_id=actor_id,
            tenant_id=_resource_id(tenant_id),
            expected_version=parse_etag(if_match),
            status=status,
            request=request,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                f"tenant.{status.lower()}",
                request,
                extra={"tenant_id": tenant_id, "if_match": if_match},
            ),
            metadata=metadata,
        )
        return _versioned_outcome(outcome, Tenant, _tenant)

    async def list_members(
        self,
        principal: AuthenticatedPrincipal,
        *,
        limit: int,
        cursor: str | None,
        metadata: RequestMetadata,
    ) -> MemberPage:
        access = await self._tenant_access(principal, "member", "list", metadata)
        records, next_cursor = await self._persistence.list_members(
            access, limit=limit, cursor=cursor
        )
        return MemberPage(
            items=[_member(record) for record in records],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    async def create_member(
        self,
        principal: AuthenticatedPrincipal,
        *,
        request: MemberCreateRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> tuple[Member, str]:
        access = await self._tenant_access(principal, "member", "create", metadata)
        _role_ids(request.role_ids)
        outcome = await self._persistence.create_member(
            access,
            issuer=principal.identity_issuer,
            request=request,
            idempotency_key=idempotency_key,
            request_hash=_request_hash("member.create", request),
            metadata=metadata,
        )
        return _versioned_outcome(outcome, Member, _member)

    async def get_member(
        self,
        principal: AuthenticatedPrincipal,
        member_id: str,
        metadata: RequestMetadata,
    ) -> tuple[Member, str]:
        access = await self._tenant_access(principal, "member", "read", metadata)
        record = await self._persistence.get_member(access, _resource_id(member_id))
        if record is None:
            raise resource_not_found()
        return _member(record), format_etag(record.resource_version)

    async def update_member(
        self,
        principal: AuthenticatedPrincipal,
        *,
        member_id: str,
        if_match: str,
        request: MemberUpdateRequest,
        metadata: RequestMetadata,
    ) -> tuple[Member, str]:
        if not request.model_fields_set:
            raise validation_error("Member update requires at least one field.")
        if request.role_ids is not None:
            _role_ids(request.role_ids)
        access = await self._tenant_access(principal, "member", "update", metadata)
        record = await self._persistence.update_member(
            access,
            member_id=_resource_id(member_id),
            expected_version=parse_etag(if_match),
            request=request,
            metadata=metadata,
        )
        if record is None:
            raise resource_not_found()
        return _member(record), format_etag(record.resource_version)

    async def delete_member(
        self,
        principal: AuthenticatedPrincipal,
        *,
        member_id: str,
        if_match: str,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> OperationAccepted:
        access = await self._tenant_access(principal, "member", "delete", metadata)
        outcome = await self._persistence.delete_member(
            access,
            member_id=_resource_id(member_id),
            expected_version=parse_etag(if_match),
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                "member.delete",
                None,
                extra={"member_id": member_id, "if_match": if_match},
            ),
            metadata=metadata,
        )
        return _operation_accepted_outcome(outcome)

    async def list_roles(
        self,
        principal: AuthenticatedPrincipal,
        *,
        limit: int,
        cursor: str | None,
        metadata: RequestMetadata,
    ) -> RolePage:
        access = await self._tenant_access(principal, "role", "list", metadata)
        records, next_cursor = await self._persistence.list_roles(
            access, limit=limit, cursor=cursor
        )
        return RolePage(
            items=[_role(record) for record in records],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    async def create_role(
        self,
        principal: AuthenticatedPrincipal,
        *,
        request: RoleCreateRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> tuple[Role, str]:
        permissions = _permissions(request.permissions)
        access = await self._tenant_access(principal, "role", "create", metadata)
        outcome = await self._persistence.create_role(
            access,
            request=request,
            permissions=permissions,
            idempotency_key=idempotency_key,
            request_hash=_request_hash("role.create", request),
            metadata=metadata,
        )
        return _versioned_outcome(outcome, Role, _role)

    async def get_role(
        self,
        principal: AuthenticatedPrincipal,
        role_id: str,
        metadata: RequestMetadata,
    ) -> tuple[Role, str]:
        access = await self._tenant_access(principal, "role", "read", metadata)
        record = await self._persistence.get_role(access, _resource_id(role_id))
        if record is None:
            raise resource_not_found()
        return _role(record), format_etag(record.resource_version)

    async def update_role(
        self,
        principal: AuthenticatedPrincipal,
        *,
        role_id: str,
        if_match: str,
        request: RoleUpdateRequest,
        metadata: RequestMetadata,
    ) -> tuple[Role, str]:
        if not request.model_fields_set:
            raise validation_error("Role update requires at least one field.")
        permissions = (
            _permissions(request.permissions)
            if request.permissions is not None
            else None
        )
        access = await self._tenant_access(principal, "role", "update", metadata)
        record = await self._persistence.update_role(
            access,
            role_id=_resource_id(role_id),
            expected_version=parse_etag(if_match),
            request=request,
            permissions=permissions,
            metadata=metadata,
        )
        if record is None:
            raise resource_not_found()
        return _role(record), format_etag(record.resource_version)

    async def delete_role(
        self,
        principal: AuthenticatedPrincipal,
        *,
        role_id: str,
        if_match: str,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> OperationAccepted:
        access = await self._tenant_access(principal, "role", "delete", metadata)
        outcome = await self._persistence.delete_role(
            access,
            role_id=_resource_id(role_id),
            expected_version=parse_etag(if_match),
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                "role.delete",
                None,
                extra={"role_id": role_id, "if_match": if_match},
            ),
            metadata=metadata,
        )
        return _operation_accepted_outcome(outcome)

    async def get_operation(
        self,
        principal: AuthenticatedPrincipal,
        operation_id: str,
        metadata: RequestMetadata,
    ) -> Operation:
        access = await self._persistence.resolve_tenant_access(principal, metadata)
        record = await self._persistence.get_operation(
            access, _resource_id(operation_id)
        )
        if record is None:
            raise resource_not_found()
        resource = record.resource_type
        if resource == "release":
            if not access.allows("agent", "read"):
                raise permission_denied()
            return _operation(record)
        if resource not in {
            "member",
            "role",
            "prompt",
            "model_provider",
            "model_config",
            "agent",
        } or not access.allows(resource, "read"):
            raise permission_denied()
        return _operation(record)

    async def _platform_actor(self, principal: AuthenticatedPrincipal) -> UUID:
        if "platform_admin" not in principal.platform_roles:
            raise permission_denied()
        actor_id = await self._persistence.resolve_platform_actor(principal)
        if actor_id is None:
            raise unauthenticated("Authenticated subject is not a platform user.")
        return actor_id

    async def _tenant_access(
        self,
        principal: AuthenticatedPrincipal,
        resource: str,
        action: str,
        metadata: RequestMetadata,
    ) -> TenantAccess:
        access = await self._persistence.resolve_tenant_access(principal, metadata)
        if not access.allows(resource, action):
            raise permission_denied()
        return access


class VersionedRecord(Protocol):
    @property
    def resource_version(self) -> int: ...


def _versioned_outcome[RecordT: VersionedRecord, ModelT: BaseModel](
    outcome: MutationOutcome[RecordT],
    model_type: type[ModelT],
    mapper: Callable[[RecordT], ModelT],
) -> tuple[ModelT, str]:
    if outcome.replay is not None:
        model = model_type.model_validate(outcome.replay.response_body)
        etag = outcome.replay.response_etag
        if etag is None:
            raise RuntimeError("versioned idempotency replay is missing ETag")
        return model, etag
    if outcome.value is None:
        raise RuntimeError("mutation outcome is missing its value")
    model = mapper(outcome.value)
    return model, format_etag(outcome.value.resource_version)


def _operation_accepted_outcome(
    outcome: MutationOutcome[OperationRecord],
) -> OperationAccepted:
    if outcome.replay is not None:
        return OperationAccepted.model_validate(outcome.replay.response_body)
    if outcome.value is None:
        raise RuntimeError("operation outcome is missing its value")
    return OperationAccepted(
        operation_id=str(outcome.value.id),
        status="ACCEPTED",
        status_url=f"/api/v1/operations/{outcome.value.id}",
    )


def _tenant(record: TenantRecord) -> Tenant:
    return Tenant(
        id=str(record.id),
        code=record.code,
        name=record.name,
        status=record.status,
        resource_version=record.resource_version,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _member(record: MemberRecord) -> Member:
    return Member(
        id=str(record.id),
        tenant_id=str(record.tenant_id),
        user_id=str(record.user_id),
        display_name=record.display_name,
        email=record.email,
        role_ids=[str(role_id) for role_id in record.role_ids],
        status=record.status,
        membership_version=record.membership_version,
        resource_version=record.resource_version,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _role(record: RoleRecord) -> Role:
    return Role(
        id=str(record.id),
        tenant_id=str(record.tenant_id),
        code=record.code,
        name=record.name,
        description=record.description,
        permissions=list(record.permissions),
        status=record.status,
        resource_version=record.resource_version,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _operation(record: OperationRecord) -> Operation:
    return Operation.model_validate(
        {
            "operation_id": str(record.id),
            "operation_type": record.operation_type,
            "status": record.status,
            "resource_type": record.resource_type,
            "resource_id": (
                str(record.resource_id) if record.resource_id is not None else None
            ),
            "result": record.result,
            "error": record.error,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "finished_at": record.finished_at,
        }
    )


def _resource_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise resource_not_found() from exc


def _role_ids(values: list[str]) -> tuple[UUID, ...]:
    try:
        parsed = tuple(UUID(value) for value in values)
    except ValueError as exc:
        raise validation_error(
            "role_ids must contain valid resource identifiers."
        ) from exc
    if len(parsed) != len(set(parsed)):
        raise validation_error("role_ids must be unique.")
    return parsed


def _permissions(values: list[str]) -> tuple[str, ...]:
    try:
        return PermissionSet.parse(values).as_strings()
    except ValueError as exc:
        raise validation_error(str(exc)) from exc


def _request_hash(
    operation_type: str,
    request: BaseModel | None,
    *,
    extra: dict[str, object] | None = None,
) -> str:
    return canonical_request_hash(
        operation_type,
        request,
        extra=extra,
        unordered_fields=("permissions", "role_ids"),
    )

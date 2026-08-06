"""SQLAlchemy persistence for IAM management use cases."""

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.public import IamPersistence, RequestMetadata
from packages.contracts.generated.resources_models import (
    ActionRequest,
    MemberCreateRequest,
    MemberUpdateRequest,
    RoleCreateRequest,
    RoleUpdateRequest,
    TenantCreateRequest,
    TenantUpdateRequest,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    SubjectType,
    TenantContext,
    permission_denied,
    resource_not_found,
    resource_state_conflict,
    resource_version_conflict,
    unauthenticated,
    validation_error,
)
from packages.domain.public import (
    MemberRecord,
    MutationOutcome,
    OperationRecord,
    OperationStatus,
    ResourceStatus,
    RoleRecord,
    TenantAccess,
    TenantRecord,
    decode_cursor,
    encode_cursor,
    format_etag,
)
from packages.infrastructure.database.idempotency import (
    claim_idempotency as _claim_idempotency,
)
from packages.infrastructure.database.idempotency import (
    complete_idempotency as _complete_idempotency,
)
from packages.infrastructure.database.models import (
    AppUserModel,
    AuditLogModel,
    OperationRecordModel,
    RoleBindingModel,
    RoleModel,
    RolePermissionModel,
    TenantMemberModel,
    TenantModel,
)
from packages.infrastructure.database.uow import PlatformUnitOfWork, TenantUnitOfWork


class SqlAlchemyIamPersistence(IamPersistence):
    """Own explicit platform and tenant transactions for IAM persistence."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def resolve_platform_actor(
        self, principal: AuthenticatedPrincipal
    ) -> UUID | None:
        async with PlatformUnitOfWork(self._session_factory) as unit_of_work:
            user = await _find_active_user(unit_of_work.session, principal)
            return user.id if user is not None else None

    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess:
        tenant_id_value = principal.active_tenant_id
        membership_version = principal.membership_version
        if tenant_id_value is None or membership_version is None:
            raise permission_denied("An active tenant membership is required.")

        async with PlatformUnitOfWork(self._session_factory) as platform_uow:
            user = await _find_active_user(platform_uow.session, principal)
        if user is None:
            raise unauthenticated("Authenticated subject is not a platform user.")

        context = TenantContext(
            tenant_id=tenant_id_value,
            subject_type=SubjectType.USER,
            subject_id=str(user.id),
            membership_version=membership_version,
            auth_time=principal.auth_time,
            request_id=metadata.request_id,
            trace_id=metadata.trace_id,
        )
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as tenant_uow:
            session = tenant_uow.session
            membership = await session.scalar(
                select(TenantMemberModel)
                .join(TenantModel, TenantModel.id == TenantMemberModel.tenant_id)
                .where(
                    TenantMemberModel.tenant_id == tenant_id,
                    TenantMemberModel.user_id == user.id,
                    TenantMemberModel.status == "ACTIVE",
                    TenantModel.id == tenant_id,
                    TenantModel.status == "ACTIVE",
                )
            )
            if membership is None:
                raise permission_denied("Active tenant membership is not available.")
            if membership.membership_version != membership_version:
                raise unauthenticated("Membership authorization is stale.")

            now = datetime.now(UTC)
            permission_rows = (
                await session.execute(
                    select(
                        RolePermissionModel.resource_type,
                        RolePermissionModel.action,
                    )
                    .join(
                        RoleModel,
                        (RoleModel.tenant_id == RolePermissionModel.tenant_id)
                        & (RoleModel.id == RolePermissionModel.role_id),
                    )
                    .join(
                        RoleBindingModel,
                        (RoleBindingModel.tenant_id == RoleModel.tenant_id)
                        & (RoleBindingModel.role_id == RoleModel.id),
                    )
                    .where(
                        RolePermissionModel.tenant_id == tenant_id,
                        RoleModel.tenant_id == tenant_id,
                        RoleModel.status == "ACTIVE",
                        RoleBindingModel.tenant_id == tenant_id,
                        RoleBindingModel.subject_type == "user",
                        RoleBindingModel.subject_id == user.id,
                        or_(
                            RoleBindingModel.expires_at.is_(None),
                            RoleBindingModel.expires_at > now,
                        ),
                    )
                )
            ).all()
        return TenantAccess(
            context=context,
            permissions=frozenset(
                f"{resource_type}:{action}" for resource_type, action in permission_rows
            ),
        )

    async def list_tenants(
        self, *, limit: int, cursor: str | None
    ) -> tuple[list[TenantRecord], str | None]:
        async with PlatformUnitOfWork(self._session_factory) as unit_of_work:
            statement = select(TenantModel).order_by(
                TenantModel.created_at.desc(), TenantModel.id.desc()
            )
            if cursor is not None:
                created_at, resource_id = _decode_cursor(cursor)
                statement = statement.where(
                    or_(
                        TenantModel.created_at < created_at,
                        and_(
                            TenantModel.created_at == created_at,
                            TenantModel.id < resource_id,
                        ),
                    )
                )
            rows = list(
                (await unit_of_work.session.scalars(statement.limit(limit + 1))).all()
            )
            page, next_cursor = _page(rows, limit)
            return [_tenant_record(row) for row in page], next_cursor

    async def create_tenant(
        self,
        *,
        actor_id: UUID,
        request: TenantCreateRequest,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[TenantRecord]:
        async with PlatformUnitOfWork(self._session_factory) as unit_of_work:
            session = unit_of_work.session
            record_id, replay = await _claim_idempotency(
                session,
                tenant_id=None,
                actor_id=actor_id,
                operation_type="tenant.create",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            existing = await session.scalar(
                select(TenantModel.id).where(TenantModel.code == request.code)
            )
            if existing is not None:
                raise resource_state_conflict("Tenant code is already in use.")
            model = TenantModel(code=request.code, name=request.name)
            session.add(model)
            await session.flush()
            result = _tenant_record(model)
            await _add_audit(
                session,
                tenant_id=model.id,
                actor_id=actor_id,
                action="resource.create",
                resource_type="tenant",
                resource_id=model.id,
                metadata=metadata,
                change={"code": model.code, "name": model.name},
            )
            await _complete_idempotency(
                session,
                record_id,
                response_status=201,
                response_body=_tenant_json(result),
                response_etag=format_etag(result.resource_version),
                response_ref=str(result.id),
            )
            return MutationOutcome(value=result)

    async def get_tenant(self, tenant_id: UUID) -> TenantRecord | None:
        async with PlatformUnitOfWork(self._session_factory) as unit_of_work:
            model = await unit_of_work.session.get(TenantModel, tenant_id)
            return _tenant_record(model) if model is not None else None

    async def update_tenant(
        self,
        *,
        actor_id: UUID,
        tenant_id: UUID,
        expected_version: int,
        request: TenantUpdateRequest,
        metadata: RequestMetadata,
    ) -> TenantRecord | None:
        async with PlatformUnitOfWork(self._session_factory) as unit_of_work:
            session = unit_of_work.session
            model = await _locked_tenant(session, tenant_id)
            if model is None:
                return None
            _check_version(model.resource_version, expected_version)
            if request.name is not None:
                model.name = request.name
            model.resource_version += 1
            model.updated_at = datetime.now(UTC)
            await session.flush()
            result = _tenant_record(model)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="resource.update",
                resource_type="tenant",
                resource_id=tenant_id,
                metadata=metadata,
                change=request.model_dump(mode="json", exclude_unset=True),
            )
            return result

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
    ) -> MutationOutcome[TenantRecord]:
        async with PlatformUnitOfWork(self._session_factory) as unit_of_work:
            session = unit_of_work.session
            operation_type = f"tenant.{status.lower()}"
            record_id, replay = await _claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type=operation_type,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            model = await _locked_tenant(session, tenant_id)
            if model is None:
                raise resource_not_found()
            _check_version(model.resource_version, expected_version)
            if model.status == status:
                raise resource_state_conflict(f"Tenant is already {status}.")
            if model.status not in {"ACTIVE", "DISABLED"}:
                raise resource_state_conflict(
                    "Tenant state does not allow this action."
                )
            model.status = status
            model.resource_version += 1
            model.updated_at = datetime.now(UTC)
            await session.execute(
                update(TenantMemberModel)
                .where(TenantMemberModel.tenant_id == tenant_id)
                .values(
                    membership_version=TenantMemberModel.membership_version + 1,
                    updated_at=datetime.now(UTC),
                )
            )
            await session.flush()
            result = _tenant_record(model)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action=(
                    "resource.disable" if status == "DISABLED" else "resource.update"
                ),
                resource_type="tenant",
                resource_id=tenant_id,
                metadata=metadata,
                change={
                    "status": status,
                    "reason": request.reason if request is not None else None,
                },
            )
            await _complete_idempotency(
                session,
                record_id,
                response_status=200,
                response_body=_tenant_json(result),
                response_etag=format_etag(result.resource_version),
                response_ref=str(result.id),
            )
            return MutationOutcome(value=result)

    async def list_members(
        self, access: TenantAccess, *, limit: int, cursor: str | None
    ) -> tuple[list[MemberRecord], str | None]:
        tenant_id = UUID(access.context.tenant_id)
        async with TenantUnitOfWork(
            self._session_factory, access.context
        ) as unit_of_work:
            statement = (
                select(TenantMemberModel, AppUserModel)
                .join(AppUserModel, AppUserModel.id == TenantMemberModel.user_id)
                .where(TenantMemberModel.tenant_id == tenant_id)
                .order_by(
                    TenantMemberModel.created_at.desc(), TenantMemberModel.id.desc()
                )
            )
            if cursor is not None:
                created_at, resource_id = _decode_cursor(cursor)
                statement = statement.where(
                    or_(
                        TenantMemberModel.created_at < created_at,
                        and_(
                            TenantMemberModel.created_at == created_at,
                            TenantMemberModel.id < resource_id,
                        ),
                    )
                )
            rows = list(
                (await unit_of_work.session.execute(statement.limit(limit + 1)))
                .tuples()
                .all()
            )
            page_rows = rows[:limit]
            role_ids = await _member_roles(
                unit_of_work.session,
                tenant_id,
                [member.user_id for member, _ in page_rows],
            )
            records = [
                _member_record(member, user, role_ids[member.user_id])
                for member, user in page_rows
            ]
            next_cursor = (
                encode_cursor(page_rows[-1][0].created_at, page_rows[-1][0].id)
                if len(rows) > limit and page_rows
                else None
            )
            return records, next_cursor

    async def create_member(
        self,
        access: TenantAccess,
        *,
        issuer: str,
        request: MemberCreateRequest,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[MemberRecord]:
        tenant_id = UUID(access.context.tenant_id)
        actor_id = UUID(access.context.subject_id)
        role_ids = tuple(UUID(role_id) for role_id in request.role_ids)
        async with TenantUnitOfWork(
            self._session_factory, access.context
        ) as unit_of_work:
            session = unit_of_work.session
            record_id, replay = await _claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type="member.create",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            await _require_active_roles(session, tenant_id, role_ids)
            user = await session.scalar(
                select(AppUserModel).where(
                    AppUserModel.identity_issuer == issuer,
                    AppUserModel.external_subject == request.external_subject,
                )
            )
            if user is None:
                user = AppUserModel(
                    identity_issuer=issuer,
                    external_subject=request.external_subject,
                    display_name=request.display_name,
                    email=request.email,
                )
                session.add(user)
                await session.flush()
            elif user.status != "ACTIVE":
                raise resource_state_conflict("Platform user is not active.")
            existing = await session.scalar(
                select(TenantMemberModel.id).where(
                    TenantMemberModel.tenant_id == tenant_id,
                    TenantMemberModel.user_id == user.id,
                )
            )
            if existing is not None:
                raise resource_state_conflict("User is already a tenant member.")
            member = TenantMemberModel(
                tenant_id=tenant_id,
                user_id=user.id,
                display_name=request.display_name,
                email=request.email,
            )
            session.add(member)
            await session.flush()
            for role_id in role_ids:
                session.add(
                    RoleBindingModel(
                        tenant_id=tenant_id,
                        subject_type="user",
                        subject_id=user.id,
                        role_id=role_id,
                    )
                )
            await session.flush()
            result = _member_record(member, user, role_ids)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="resource.create",
                resource_type="member",
                resource_id=member.id,
                metadata=metadata,
                change={"external_subject": request.external_subject},
            )
            for role_id in role_ids:
                await _add_audit(
                    session,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    action="role.bind",
                    resource_type="member",
                    resource_id=member.id,
                    metadata=metadata,
                    change={"role_id": str(role_id)},
                )
            await _complete_idempotency(
                session,
                record_id,
                response_status=201,
                response_body=_member_json(result),
                response_etag=format_etag(result.resource_version),
                response_ref=str(result.id),
            )
            return MutationOutcome(value=result)

    async def get_member(
        self, access: TenantAccess, member_id: UUID
    ) -> MemberRecord | None:
        tenant_id = UUID(access.context.tenant_id)
        async with TenantUnitOfWork(
            self._session_factory, access.context
        ) as unit_of_work:
            session = unit_of_work.session
            row = (
                await session.execute(
                    select(TenantMemberModel, AppUserModel)
                    .join(AppUserModel, AppUserModel.id == TenantMemberModel.user_id)
                    .where(
                        TenantMemberModel.tenant_id == tenant_id,
                        TenantMemberModel.id == member_id,
                    )
                )
            ).one_or_none()
            if row is None:
                await _audit_scope_denied(session, access, "member", member_id)
                return None
            member, user = row
            role_ids = await _member_roles(session, tenant_id, [member.user_id])
            return _member_record(member, user, role_ids[member.user_id])

    async def update_member(
        self,
        access: TenantAccess,
        *,
        member_id: UUID,
        expected_version: int,
        request: MemberUpdateRequest,
        metadata: RequestMetadata,
    ) -> MemberRecord | None:
        tenant_id = UUID(access.context.tenant_id)
        actor_id = UUID(access.context.subject_id)
        async with TenantUnitOfWork(
            self._session_factory, access.context
        ) as unit_of_work:
            session = unit_of_work.session
            row = (
                await session.execute(
                    select(TenantMemberModel, AppUserModel)
                    .join(AppUserModel, AppUserModel.id == TenantMemberModel.user_id)
                    .where(
                        TenantMemberModel.tenant_id == tenant_id,
                        TenantMemberModel.id == member_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if row is None:
                await _audit_scope_denied(session, access, "member", member_id)
                return None
            member, user = row
            _check_version(member.resource_version, expected_version)
            old_roles = set(
                await session.scalars(
                    select(RoleBindingModel.role_id).where(
                        RoleBindingModel.tenant_id == tenant_id,
                        RoleBindingModel.subject_type == "user",
                        RoleBindingModel.subject_id == member.user_id,
                    )
                )
            )
            new_roles = old_roles
            if request.role_ids is not None:
                new_roles = {UUID(role_id) for role_id in request.role_ids}
                await _require_active_roles(session, tenant_id, tuple(new_roles))
                removed = old_roles - new_roles
                added = new_roles - old_roles
                if removed:
                    await session.execute(
                        delete(RoleBindingModel).where(
                            RoleBindingModel.tenant_id == tenant_id,
                            RoleBindingModel.subject_type == "user",
                            RoleBindingModel.subject_id == member.user_id,
                            RoleBindingModel.role_id.in_(removed),
                        )
                    )
                for role_id in added:
                    session.add(
                        RoleBindingModel(
                            tenant_id=tenant_id,
                            subject_type="user",
                            subject_id=member.user_id,
                            role_id=role_id,
                        )
                    )
                for role_id in sorted(removed):
                    await _add_audit(
                        session,
                        tenant_id=tenant_id,
                        actor_id=actor_id,
                        action="role.unbind",
                        resource_type="member",
                        resource_id=member.id,
                        metadata=metadata,
                        change={"role_id": str(role_id)},
                    )
                for role_id in sorted(added):
                    await _add_audit(
                        session,
                        tenant_id=tenant_id,
                        actor_id=actor_id,
                        action="role.bind",
                        resource_type="member",
                        resource_id=member.id,
                        metadata=metadata,
                        change={"role_id": str(role_id)},
                    )
            if request.display_name is not None:
                member.display_name = request.display_name
            if request.status is not None:
                member.status = request.status
                member.disabled_at = (
                    datetime.now(UTC) if request.status == "DISABLED" else None
                )
            member.membership_version += 1
            member.resource_version += 1
            member.updated_at = datetime.now(UTC)
            await session.flush()
            result = _member_record(member, user, tuple(sorted(new_roles)))
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action=(
                    "resource.disable"
                    if request.status == "DISABLED"
                    else "resource.update"
                ),
                resource_type="member",
                resource_id=member.id,
                metadata=metadata,
                change=request.model_dump(mode="json", exclude_unset=True),
            )
            return result

    async def delete_member(
        self,
        access: TenantAccess,
        *,
        member_id: UUID,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[OperationRecord]:
        tenant_id = UUID(access.context.tenant_id)
        actor_id = UUID(access.context.subject_id)
        async with TenantUnitOfWork(
            self._session_factory, access.context
        ) as unit_of_work:
            session = unit_of_work.session
            record_id, replay = await _claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type="member.delete",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            member = await session.scalar(
                select(TenantMemberModel)
                .where(
                    TenantMemberModel.tenant_id == tenant_id,
                    TenantMemberModel.id == member_id,
                )
                .with_for_update()
            )
            if member is None:
                raise resource_not_found()
            _check_version(member.resource_version, expected_version)
            if member.status in {"DELETING", "DELETED"}:
                raise resource_state_conflict("Member is already being deleted.")
            role_ids = list(
                await session.scalars(
                    select(RoleBindingModel.role_id).where(
                        RoleBindingModel.tenant_id == tenant_id,
                        RoleBindingModel.subject_type == "user",
                        RoleBindingModel.subject_id == member.user_id,
                    )
                )
            )
            await session.execute(
                delete(RoleBindingModel).where(
                    RoleBindingModel.tenant_id == tenant_id,
                    RoleBindingModel.subject_type == "user",
                    RoleBindingModel.subject_id == member.user_id,
                )
            )
            member.status = "DELETED"
            member.disabled_at = datetime.now(UTC)
            member.membership_version += 1
            member.resource_version += 1
            member.updated_at = datetime.now(UTC)
            operation = OperationRecordModel(
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type="member.delete",
                status="SUCCEEDED",
                resource_type="member",
                resource_id=member.id,
                result_json={"deleted": True},
                finished_at=datetime.now(UTC),
            )
            session.add(operation)
            await session.flush()
            result = _operation_record(operation)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="resource.delete",
                resource_type="member",
                resource_id=member.id,
                metadata=metadata,
                change={"status": "DELETED"},
            )
            for role_id in role_ids:
                await _add_audit(
                    session,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    action="role.unbind",
                    resource_type="member",
                    resource_id=member.id,
                    metadata=metadata,
                    change={"role_id": str(role_id)},
                )
            response_body = _operation_accepted_json(result)
            await _complete_idempotency(
                session,
                record_id,
                response_status=202,
                response_body=response_body,
                response_etag=None,
                response_ref=str(result.id),
            )
            return MutationOutcome(value=result)

    async def list_roles(
        self, access: TenantAccess, *, limit: int, cursor: str | None
    ) -> tuple[list[RoleRecord], str | None]:
        tenant_id = UUID(access.context.tenant_id)
        async with TenantUnitOfWork(
            self._session_factory, access.context
        ) as unit_of_work:
            statement = (
                select(RoleModel)
                .where(RoleModel.tenant_id == tenant_id)
                .order_by(RoleModel.created_at.desc(), RoleModel.id.desc())
            )
            if cursor is not None:
                created_at, resource_id = _decode_cursor(cursor)
                statement = statement.where(
                    or_(
                        RoleModel.created_at < created_at,
                        and_(
                            RoleModel.created_at == created_at,
                            RoleModel.id < resource_id,
                        ),
                    )
                )
            rows = list(
                (await unit_of_work.session.scalars(statement.limit(limit + 1))).all()
            )
            page_rows = rows[:limit]
            permissions = await _role_permissions(
                unit_of_work.session, tenant_id, [role.id for role in page_rows]
            )
            records = [_role_record(role, permissions[role.id]) for role in page_rows]
            next_cursor = (
                encode_cursor(page_rows[-1].created_at, page_rows[-1].id)
                if len(rows) > limit and page_rows
                else None
            )
            return records, next_cursor

    async def create_role(
        self,
        access: TenantAccess,
        *,
        request: RoleCreateRequest,
        permissions: tuple[str, ...],
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[RoleRecord]:
        tenant_id = UUID(access.context.tenant_id)
        actor_id = UUID(access.context.subject_id)
        async with TenantUnitOfWork(
            self._session_factory, access.context
        ) as unit_of_work:
            session = unit_of_work.session
            record_id, replay = await _claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type="role.create",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            existing = await session.scalar(
                select(RoleModel.id).where(
                    RoleModel.tenant_id == tenant_id, RoleModel.code == request.code
                )
            )
            if existing is not None:
                raise resource_state_conflict("Role code is already in use.")
            role = RoleModel(
                tenant_id=tenant_id,
                code=request.code,
                name=request.name,
                description=request.description,
            )
            session.add(role)
            await session.flush()
            _set_role_permissions(session, tenant_id, role.id, permissions)
            await session.flush()
            result = _role_record(role, permissions)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="resource.create",
                resource_type="role",
                resource_id=role.id,
                metadata=metadata,
                change=request.model_dump(mode="json"),
            )
            await _complete_idempotency(
                session,
                record_id,
                response_status=201,
                response_body=_role_json(result),
                response_etag=format_etag(result.resource_version),
                response_ref=str(result.id),
            )
            return MutationOutcome(value=result)

    async def get_role(self, access: TenantAccess, role_id: UUID) -> RoleRecord | None:
        tenant_id = UUID(access.context.tenant_id)
        async with TenantUnitOfWork(
            self._session_factory, access.context
        ) as unit_of_work:
            session = unit_of_work.session
            role = await session.scalar(
                select(RoleModel).where(
                    RoleModel.tenant_id == tenant_id, RoleModel.id == role_id
                )
            )
            if role is None:
                await _audit_scope_denied(session, access, "role", role_id)
                return None
            permissions = await _role_permissions(session, tenant_id, [role.id])
            return _role_record(role, permissions[role.id])

    async def update_role(
        self,
        access: TenantAccess,
        *,
        role_id: UUID,
        expected_version: int,
        request: RoleUpdateRequest,
        permissions: tuple[str, ...] | None,
        metadata: RequestMetadata,
    ) -> RoleRecord | None:
        tenant_id = UUID(access.context.tenant_id)
        actor_id = UUID(access.context.subject_id)
        async with TenantUnitOfWork(
            self._session_factory, access.context
        ) as unit_of_work:
            session = unit_of_work.session
            role = await session.scalar(
                select(RoleModel)
                .where(RoleModel.tenant_id == tenant_id, RoleModel.id == role_id)
                .with_for_update()
            )
            if role is None:
                await _audit_scope_denied(session, access, "role", role_id)
                return None
            _check_version(role.resource_version, expected_version)
            if request.name is not None:
                role.name = request.name
            if "description" in request.model_fields_set:
                role.description = request.description
            if request.status is not None:
                role.status = request.status
            if permissions is not None:
                await session.execute(
                    delete(RolePermissionModel).where(
                        RolePermissionModel.tenant_id == tenant_id,
                        RolePermissionModel.role_id == role.id,
                    )
                )
                _set_role_permissions(session, tenant_id, role.id, permissions)
            role.resource_version += 1
            role.updated_at = datetime.now(UTC)
            await _increment_members_for_role(session, tenant_id, role.id)
            await session.flush()
            effective_permissions = (
                permissions
                if permissions is not None
                else (await _role_permissions(session, tenant_id, [role.id]))[role.id]
            )
            result = _role_record(role, effective_permissions)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action=(
                    "resource.disable"
                    if request.status == "DISABLED"
                    else "resource.update"
                ),
                resource_type="role",
                resource_id=role.id,
                metadata=metadata,
                change=request.model_dump(mode="json", exclude_unset=True),
            )
            return result

    async def delete_role(
        self,
        access: TenantAccess,
        *,
        role_id: UUID,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[OperationRecord]:
        tenant_id = UUID(access.context.tenant_id)
        actor_id = UUID(access.context.subject_id)
        async with TenantUnitOfWork(
            self._session_factory, access.context
        ) as unit_of_work:
            session = unit_of_work.session
            record_id, replay = await _claim_idempotency(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type="role.delete",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return MutationOutcome(replay=replay)
            role = await session.scalar(
                select(RoleModel)
                .where(RoleModel.tenant_id == tenant_id, RoleModel.id == role_id)
                .with_for_update()
            )
            if role is None:
                raise resource_not_found()
            _check_version(role.resource_version, expected_version)
            if role.built_in:
                raise resource_state_conflict("Built-in roles cannot be deleted.")
            if role.status in {"DELETING", "DELETED"}:
                raise resource_state_conflict("Role is already being deleted.")
            subject_ids = list(
                await session.scalars(
                    select(RoleBindingModel.subject_id).where(
                        RoleBindingModel.tenant_id == tenant_id,
                        RoleBindingModel.role_id == role.id,
                        RoleBindingModel.subject_type == "user",
                    )
                )
            )
            await _increment_members_for_role(session, tenant_id, role.id)
            await session.execute(
                delete(RoleBindingModel).where(
                    RoleBindingModel.tenant_id == tenant_id,
                    RoleBindingModel.role_id == role.id,
                )
            )
            await session.execute(
                delete(RolePermissionModel).where(
                    RolePermissionModel.tenant_id == tenant_id,
                    RolePermissionModel.role_id == role.id,
                )
            )
            role.status = "DELETED"
            role.resource_version += 1
            role.updated_at = datetime.now(UTC)
            operation = OperationRecordModel(
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation_type="role.delete",
                status="SUCCEEDED",
                resource_type="role",
                resource_id=role.id,
                result_json={"deleted": True},
                finished_at=datetime.now(UTC),
            )
            session.add(operation)
            await session.flush()
            result = _operation_record(operation)
            await _add_audit(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="resource.delete",
                resource_type="role",
                resource_id=role.id,
                metadata=metadata,
                change={"status": "DELETED"},
            )
            for subject_id in subject_ids:
                await _add_audit(
                    session,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    action="role.unbind",
                    resource_type="role",
                    resource_id=role.id,
                    metadata=metadata,
                    change={"subject_id": str(subject_id)},
                )
            await _complete_idempotency(
                session,
                record_id,
                response_status=202,
                response_body=_operation_accepted_json(result),
                response_etag=None,
                response_ref=str(result.id),
            )
            return MutationOutcome(value=result)

    async def get_operation(
        self, access: TenantAccess, operation_id: UUID
    ) -> OperationRecord | None:
        tenant_id = UUID(access.context.tenant_id)
        async with TenantUnitOfWork(
            self._session_factory, access.context
        ) as unit_of_work:
            operation = await unit_of_work.session.scalar(
                select(OperationRecordModel).where(
                    OperationRecordModel.tenant_id == tenant_id,
                    OperationRecordModel.id == operation_id,
                )
            )
            return _operation_record(operation) if operation is not None else None


async def _find_active_user(
    session: AsyncSession, principal: AuthenticatedPrincipal
) -> AppUserModel | None:
    return await session.scalar(
        select(AppUserModel).where(
            AppUserModel.identity_issuer == principal.identity_issuer,
            AppUserModel.external_subject == principal.external_subject,
            AppUserModel.status == "ACTIVE",
        )
    )


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        return decode_cursor(cursor)
    except ValueError as exc:
        raise validation_error("Pagination cursor is invalid.") from exc


def _page(rows: list[TenantModel], limit: int) -> tuple[list[TenantModel], str | None]:
    page_rows = rows[:limit]
    next_cursor = (
        encode_cursor(page_rows[-1].created_at, page_rows[-1].id)
        if len(rows) > limit and page_rows
        else None
    )
    return page_rows, next_cursor


async def _locked_tenant(session: AsyncSession, tenant_id: UUID) -> TenantModel | None:
    return await session.scalar(
        select(TenantModel).where(TenantModel.id == tenant_id).with_for_update()
    )


def _check_version(current: int, expected: int) -> None:
    if current != expected:
        raise resource_version_conflict()


async def _require_active_roles(
    session: AsyncSession, tenant_id: UUID, role_ids: tuple[UUID, ...]
) -> None:
    if not role_ids:
        return
    found = set(
        await session.scalars(
            select(RoleModel.id).where(
                RoleModel.tenant_id == tenant_id,
                RoleModel.id.in_(role_ids),
                RoleModel.status == "ACTIVE",
            )
        )
    )
    if found != set(role_ids):
        raise resource_not_found("One or more roles were not found.")


async def _member_roles(
    session: AsyncSession, tenant_id: UUID, user_ids: list[UUID]
) -> defaultdict[UUID, tuple[UUID, ...]]:
    result: defaultdict[UUID, list[UUID]] = defaultdict(list)
    if user_ids:
        rows = (
            await session.execute(
                select(RoleBindingModel.subject_id, RoleBindingModel.role_id).where(
                    RoleBindingModel.tenant_id == tenant_id,
                    RoleBindingModel.subject_type == "user",
                    RoleBindingModel.subject_id.in_(user_ids),
                )
            )
        ).all()
        for subject_id, role_id in rows:
            result[subject_id].append(role_id)
    return defaultdict(
        tuple, {key: tuple(sorted(value)) for key, value in result.items()}
    )


async def _role_permissions(
    session: AsyncSession, tenant_id: UUID, role_ids: list[UUID]
) -> defaultdict[UUID, tuple[str, ...]]:
    result: defaultdict[UUID, list[str]] = defaultdict(list)
    if role_ids:
        rows = (
            await session.execute(
                select(
                    RolePermissionModel.role_id,
                    RolePermissionModel.resource_type,
                    RolePermissionModel.action,
                ).where(
                    RolePermissionModel.tenant_id == tenant_id,
                    RolePermissionModel.role_id.in_(role_ids),
                )
            )
        ).all()
        for role_id, resource_type, action in rows:
            result[role_id].append(f"{resource_type}:{action}")
    return defaultdict(
        tuple, {key: tuple(sorted(value)) for key, value in result.items()}
    )


def _set_role_permissions(
    session: AsyncSession,
    tenant_id: UUID,
    role_id: UUID,
    permissions: tuple[str, ...],
) -> None:
    for permission in permissions:
        resource_type, action = permission.split(":", maxsplit=1)
        session.add(
            RolePermissionModel(
                tenant_id=tenant_id,
                role_id=role_id,
                resource_type=resource_type,
                action=action,
            )
        )


async def _increment_members_for_role(
    session: AsyncSession, tenant_id: UUID, role_id: UUID
) -> None:
    user_ids = select(RoleBindingModel.subject_id).where(
        RoleBindingModel.tenant_id == tenant_id,
        RoleBindingModel.role_id == role_id,
        RoleBindingModel.subject_type == "user",
    )
    await session.execute(
        update(TenantMemberModel)
        .where(
            TenantMemberModel.tenant_id == tenant_id,
            TenantMemberModel.user_id.in_(user_ids),
        )
        .values(
            membership_version=TenantMemberModel.membership_version + 1,
            updated_at=datetime.now(UTC),
        )
    )


async def _add_audit(
    session: AsyncSession,
    *,
    tenant_id: UUID | None,
    actor_id: UUID,
    action: str,
    resource_type: str,
    resource_id: UUID | None,
    metadata: RequestMetadata,
    change: dict[str, object],
    result: str = "SUCCESS",
    reason_codes: list[str] | None = None,
) -> None:
    canonical = json.dumps(change, sort_keys=True, separators=(",", ":")).encode()
    session.add(
        AuditLogModel(
            tenant_id=tenant_id,
            actor_type="user",
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            result=result,
            reason_codes=reason_codes or [],
            change_digest=f"sha256:{hashlib.sha256(canonical).hexdigest()}",
            request_id=metadata.request_id,
            trace_id=metadata.trace_id,
            metadata_json=change,
        )
    )


async def _audit_scope_denied(
    session: AsyncSession,
    access: TenantAccess,
    resource_type: str,
    resource_id: UUID,
) -> None:
    await _add_audit(
        session,
        tenant_id=UUID(access.context.tenant_id),
        actor_id=UUID(access.context.subject_id),
        action="security.cross_tenant_denied",
        resource_type=resource_type,
        resource_id=resource_id,
        metadata=RequestMetadata(
            request_id=access.context.request_id,
            trace_id=access.context.trace_id,
        ),
        change={"attempted_resource_id": str(resource_id)},
        result="DENIED",
        reason_codes=["RESOURCE_SCOPE_UNAVAILABLE"],
    )


def _tenant_record(model: TenantModel) -> TenantRecord:
    return TenantRecord(
        id=model.id,
        code=model.code,
        name=model.name,
        status=cast(ResourceStatus, model.status),
        resource_version=model.resource_version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _member_record(
    member: TenantMemberModel,
    user: AppUserModel,
    role_ids: tuple[UUID, ...],
) -> MemberRecord:
    return MemberRecord(
        id=member.id,
        tenant_id=member.tenant_id,
        user_id=member.user_id,
        display_name=member.display_name or user.display_name,
        email=member.email if member.email is not None else user.email,
        role_ids=tuple(sorted(role_ids)),
        status=cast(ResourceStatus, member.status),
        membership_version=member.membership_version,
        resource_version=member.resource_version,
        created_at=member.created_at,
        updated_at=member.updated_at,
    )


def _role_record(role: RoleModel, permissions: tuple[str, ...]) -> RoleRecord:
    return RoleRecord(
        id=role.id,
        tenant_id=role.tenant_id,
        code=role.code,
        name=role.name,
        description=role.description,
        permissions=tuple(sorted(permissions)),
        status=cast(ResourceStatus, role.status),
        built_in=role.built_in,
        resource_version=role.resource_version,
        created_at=role.created_at,
        updated_at=role.updated_at,
    )


def _operation_record(model: OperationRecordModel) -> OperationRecord:
    return OperationRecord(
        id=model.id,
        operation_type=model.operation_type,
        status=cast(OperationStatus, model.status),
        resource_type=model.resource_type,
        resource_id=model.resource_id,
        result=cast(dict[str, JsonValue] | None, model.result_json),
        error=cast(dict[str, JsonValue] | None, model.error_json),
        created_at=model.created_at,
        updated_at=model.updated_at,
        finished_at=model.finished_at,
    )


def _tenant_json(record: TenantRecord) -> dict[str, object]:
    return {
        "id": str(record.id),
        "code": record.code,
        "name": record.name,
        "status": record.status,
        "resource_version": record.resource_version,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def _member_json(record: MemberRecord) -> dict[str, object]:
    return {
        "id": str(record.id),
        "tenant_id": str(record.tenant_id),
        "user_id": str(record.user_id),
        "display_name": record.display_name,
        "email": record.email,
        "role_ids": [str(role_id) for role_id in record.role_ids],
        "status": record.status,
        "membership_version": record.membership_version,
        "resource_version": record.resource_version,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def _role_json(record: RoleRecord) -> dict[str, object]:
    return {
        "id": str(record.id),
        "tenant_id": str(record.tenant_id),
        "code": record.code,
        "name": record.name,
        "description": record.description,
        "permissions": list(record.permissions),
        "status": record.status,
        "resource_version": record.resource_version,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def _operation_accepted_json(record: OperationRecord) -> dict[str, object]:
    return {
        "operation_id": str(record.id),
        "status": "ACCEPTED",
        "status_url": f"/api/v1/operations/{record.id}",
    }

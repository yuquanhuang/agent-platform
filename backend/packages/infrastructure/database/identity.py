"""Privileged identity reader used only for authoritative `/me` lookup."""

from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.contracts.public import AuthenticatedPrincipal
from packages.domain.public import IdentitySnapshot, MembershipSnapshot
from packages.infrastructure.database.models import (
    AppUserModel,
    RoleBindingModel,
    RoleModel,
    TenantMemberModel,
    TenantModel,
)
from packages.infrastructure.database.uow import PlatformUnitOfWork


class SqlAlchemyIdentityReader:
    """Read a user's own memberships through an explicit platform DB boundary."""

    def __init__(
        self,
        platform_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._platform_session_factory = platform_session_factory

    async def read(self, principal: AuthenticatedPrincipal) -> IdentitySnapshot | None:
        async with PlatformUnitOfWork(self._platform_session_factory) as unit_of_work:
            user = await unit_of_work.session.scalar(
                select(AppUserModel).where(
                    AppUserModel.identity_issuer == principal.identity_issuer,
                    AppUserModel.external_subject == principal.external_subject,
                    AppUserModel.status == "ACTIVE",
                )
            )
            if user is None:
                return None

            membership_rows = (
                await unit_of_work.session.execute(
                    select(TenantMemberModel, TenantModel)
                    .join(TenantModel, TenantModel.id == TenantMemberModel.tenant_id)
                    .where(
                        TenantMemberModel.user_id == user.id,
                        TenantMemberModel.status.in_(("ACTIVE", "DISABLED")),
                        TenantModel.status.in_(("ACTIVE", "DISABLED")),
                    )
                    .order_by(TenantModel.created_at, TenantModel.id)
                )
            ).all()

            roles_by_tenant = await self._read_active_roles(
                unit_of_work.session, user.id
            )
            memberships = tuple(
                MembershipSnapshot(
                    tenant_id=str(member.tenant_id),
                    tenant_name=tenant.name,
                    status=(
                        "ACTIVE"
                        if member.status == "ACTIVE" and tenant.status == "ACTIVE"
                        else "DISABLED"
                    ),
                    role_ids=tuple(
                        sorted(
                            str(role_id)
                            for role_id in roles_by_tenant[member.tenant_id]
                        )
                    ),
                    membership_version=member.membership_version,
                )
                for member, tenant in membership_rows
            )
            return IdentitySnapshot(
                user_id=str(user.id),
                external_subject=user.external_subject,
                display_name=user.display_name,
                auth_time=principal.auth_time,
                memberships=memberships,
            )

    async def _read_active_roles(
        self, session: AsyncSession, user_id: UUID
    ) -> defaultdict[UUID, list[UUID]]:
        now = datetime.now(UTC)
        role_rows = (
            await session.execute(
                select(RoleBindingModel.tenant_id, RoleBindingModel.role_id)
                .join(
                    RoleModel,
                    (RoleModel.tenant_id == RoleBindingModel.tenant_id)
                    & (RoleModel.id == RoleBindingModel.role_id),
                )
                .where(
                    RoleBindingModel.subject_type == "user",
                    RoleBindingModel.subject_id == user_id,
                    RoleModel.status == "ACTIVE",
                    or_(
                        RoleBindingModel.expires_at.is_(None),
                        RoleBindingModel.expires_at > now,
                    ),
                )
            )
        ).all()
        roles_by_tenant: defaultdict[UUID, list[UUID]] = defaultdict(list)
        for tenant_id, role_id in role_rows:
            roles_by_tenant[tenant_id].append(role_id)
        return roles_by_tenant

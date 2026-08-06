"""Current identity use case and persistence port."""

from typing import Protocol

from packages.contracts.generated.core_models import (
    CurrentIdentity,
    CurrentIdentityMembershipsItem,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    permission_denied,
    unauthenticated,
)
from packages.domain.public import IdentitySnapshot


class IdentityReader(Protocol):
    async def read(self, principal: AuthenticatedPrincipal) -> IdentitySnapshot | None:
        """Load current authoritative membership and role state."""


class CurrentIdentityService:
    """Validate server claims against current membership state and return `/me`."""

    def __init__(self, identity_reader: IdentityReader) -> None:
        self._identity_reader = identity_reader

    async def get_current_identity(
        self, principal: AuthenticatedPrincipal
    ) -> CurrentIdentity:
        snapshot = await self._identity_reader.read(principal)
        if snapshot is None:
            raise unauthenticated("Authenticated subject is not a platform user.")

        active_tenant_id = principal.active_tenant_id
        if active_tenant_id is not None:
            active_membership = next(
                (
                    membership
                    for membership in snapshot.memberships
                    if membership.tenant_id == active_tenant_id
                ),
                None,
            )
            if active_membership is None or active_membership.status != "ACTIVE":
                raise permission_denied("Active tenant membership is not available.")
            if principal.membership_version != active_membership.membership_version:
                raise unauthenticated("Membership authorization is stale.")

        memberships = [
            CurrentIdentityMembershipsItem(
                tenant_id=membership.tenant_id,
                tenant_name=membership.tenant_name,
                status=membership.status,
                role_ids=list(membership.role_ids),
                membership_version=membership.membership_version,
            )
            for membership in snapshot.memberships
        ]
        return CurrentIdentity(
            user_id=snapshot.user_id,
            external_subject=snapshot.external_subject,
            display_name=snapshot.display_name,
            active_tenant_id=active_tenant_id,
            memberships=memberships,
            auth_time=principal.auth_time,
        )

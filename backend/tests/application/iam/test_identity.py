"""Authoritative current identity use case tests."""

from datetime import UTC, datetime
from typing import Literal

import pytest

from packages.application.public import CurrentIdentityService
from packages.contracts.public import AuthenticatedPrincipal, PlatformError
from packages.domain.public import IdentitySnapshot, MembershipSnapshot

TENANT_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"
ROLE_ID = "33333333-3333-4333-8333-333333333333"
AUTH_TIME = datetime(2026, 8, 6, tzinfo=UTC)


class StubIdentityReader:
    def __init__(self, snapshot: IdentitySnapshot | None) -> None:
        self.snapshot = snapshot

    async def read(self, principal: AuthenticatedPrincipal) -> IdentitySnapshot | None:
        return self.snapshot


def principal(*, membership_version: int = 3) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        identity_issuer="https://mock.agent-platform.test/",
        external_subject="mock-user",
        display_name="Mock User",
        platform_roles=frozenset(),
        active_tenant_id=TENANT_ID,
        membership_version=membership_version,
        auth_time=AUTH_TIME,
    )


def snapshot(*, status: Literal["ACTIVE", "DISABLED"] = "ACTIVE") -> IdentitySnapshot:
    return IdentitySnapshot(
        user_id=USER_ID,
        external_subject="mock-user",
        display_name="Mock User",
        auth_time=AUTH_TIME,
        memberships=(
            MembershipSnapshot(
                tenant_id=TENANT_ID,
                tenant_name="Tenant A",
                status=status,
                role_ids=(ROLE_ID,),
                membership_version=3,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_current_identity_returns_database_membership_and_roles() -> None:
    service = CurrentIdentityService(StubIdentityReader(snapshot()))

    identity = await service.get_current_identity(principal())

    assert identity.user_id == USER_ID
    assert identity.active_tenant_id == TENANT_ID
    assert identity.memberships[0].role_ids == [ROLE_ID]
    assert identity.memberships[0].membership_version == 3


@pytest.mark.asyncio
async def test_current_identity_rejects_stale_membership_claim() -> None:
    service = CurrentIdentityService(StubIdentityReader(snapshot()))

    with pytest.raises(PlatformError) as error:
        await service.get_current_identity(principal(membership_version=2))

    assert error.value.status_code == 401
    assert error.value.code == "UNAUTHENTICATED"


@pytest.mark.asyncio
async def test_current_identity_rejects_disabled_membership() -> None:
    service = CurrentIdentityService(StubIdentityReader(snapshot(status="DISABLED")))

    with pytest.raises(PlatformError) as error:
        await service.get_current_identity(principal())

    assert error.value.status_code == 403
    assert error.value.code == "PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_current_identity_rejects_unknown_platform_user() -> None:
    service = CurrentIdentityService(StubIdentityReader(None))

    with pytest.raises(PlatformError) as error:
        await service.get_current_identity(principal())

    assert error.value.status_code == 401

"""Real PostgreSQL current identity and membership invalidation verification."""

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from packages.application.public import CurrentIdentityService
from packages.contracts.public import AuthenticatedPrincipal, PlatformError
from packages.infrastructure.database.public import (
    SqlAlchemyIdentityReader,
    create_session_factory,
)

BACKEND_ROOT = Path(__file__).resolve().parents[3]
DATABASE_URL_ENV = "AP_TEST_DATABASE_URL"
TENANT_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"
MEMBER_ID = "33333333-3333-4333-8333-333333333333"
ROLE_ID = "44444444-4444-4444-8444-444444444444"


def require_database_url() -> str:
    database_url = os.getenv(DATABASE_URL_ENV)
    if database_url is None:
        pytest.skip(f"{DATABASE_URL_ENV} is required for PostgreSQL integration tests")
    database_name = make_url(database_url).database or ""
    if not database_name.endswith("_test"):
        pytest.fail(f"{DATABASE_URL_ENV} must target a dedicated *_test database")
    return database_url


def migrate(database_url: str, revision: str) -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    if revision == "base":
        command.downgrade(config, revision)
    else:
        command.upgrade(config, revision)


def principal(*, membership_version: int) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        identity_issuer="https://mock.agent-platform.test/",
        external_subject="mock-platform-admin",
        display_name="Mock Platform Admin",
        email="mock-admin@example.test",
        platform_roles=frozenset({"platform_admin"}),
        active_tenant_id=TENANT_ID,
        membership_version=membership_version,
        auth_time=datetime(2026, 8, 6, tzinfo=UTC),
    )


async def verify_current_identity(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenant (id, code, name) "
                    "VALUES (:tenant_id, 'tenant-a', 'Tenant A')"
                ),
                {"tenant_id": TENANT_ID},
            )
            await connection.execute(
                text(
                    "INSERT INTO app_user "
                    "(id, identity_issuer, external_subject, display_name, email) "
                    "VALUES (:user_id, :issuer, 'mock-platform-admin', "
                    "'Mock Platform Admin', 'mock-admin@example.test')"
                ),
                {
                    "user_id": USER_ID,
                    "issuer": "https://mock.agent-platform.test/",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO tenant_member "
                    "(id, tenant_id, user_id, membership_version) "
                    "VALUES (:member_id, :tenant_id, :user_id, 1)"
                ),
                {
                    "member_id": MEMBER_ID,
                    "tenant_id": TENANT_ID,
                    "user_id": USER_ID,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO role (id, tenant_id, code, name, built_in) "
                    "VALUES (:role_id, :tenant_id, 'tenant_admin', "
                    "'Tenant Admin', true)"
                ),
                {"role_id": ROLE_ID, "tenant_id": TENANT_ID},
            )
            await connection.execute(
                text(
                    "INSERT INTO role_binding "
                    "(tenant_id, subject_type, subject_id, role_id) "
                    "VALUES (:tenant_id, 'user', :user_id, :role_id)"
                ),
                {
                    "tenant_id": TENANT_ID,
                    "user_id": USER_ID,
                    "role_id": ROLE_ID,
                },
            )

        reader = SqlAlchemyIdentityReader(create_session_factory(engine))
        service = CurrentIdentityService(reader)

        identity = await service.get_current_identity(principal(membership_version=1))
        assert identity.active_tenant_id == TENANT_ID
        assert identity.memberships[0].role_ids == [ROLE_ID]
        assert identity.memberships[0].membership_version == 1

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE tenant_member SET membership_version = 2 "
                    "WHERE id = :member_id"
                ),
                {"member_id": MEMBER_ID},
            )

        with pytest.raises(PlatformError) as stale_error:
            await service.get_current_identity(principal(membership_version=1))
        assert stale_error.value.code == "UNAUTHENTICATED"

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE tenant_member SET status = 'DISABLED' "
                    "WHERE id = :member_id"
                ),
                {"member_id": MEMBER_ID},
            )

        with pytest.raises(PlatformError) as disabled_error:
            await service.get_current_identity(principal(membership_version=2))
        assert disabled_error.value.code == "PERMISSION_DENIED"
    finally:
        await engine.dispose()


def test_postgresql_current_identity_and_membership_invalidation() -> None:
    database_url = require_database_url()
    migrate(database_url, "base")
    try:
        migrate(database_url, "head")
        asyncio.run(verify_current_identity(database_url))
    finally:
        migrate(database_url, "base")

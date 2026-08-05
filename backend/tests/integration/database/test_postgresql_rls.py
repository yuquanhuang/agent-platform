"""Real PostgreSQL migration and RLS isolation verification."""

import asyncio
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

BACKEND_ROOT = Path(__file__).resolve().parents[3]
DATABASE_URL_ENV = "AP_TEST_DATABASE_URL"
APP_ROLE = "agent_platform_rls_test"
APP_PASSWORD = "rls-test-only"
TENANT_A = "11111111-1111-4111-8111-111111111111"
TENANT_B = "22222222-2222-4222-8222-222222222222"
USER_A = "33333333-3333-4333-8333-333333333333"
MEMBER_A = "44444444-4444-4444-8444-444444444444"


def require_database_url() -> str:
    database_url = os.getenv(DATABASE_URL_ENV)
    if database_url is None:
        pytest.skip(f"{DATABASE_URL_ENV} is required for PostgreSQL integration tests")
    database_name = make_url(database_url).database or ""
    if not database_name.endswith("_test"):
        pytest.fail(f"{DATABASE_URL_ENV} must target a dedicated *_test database")
    return database_url


def migrate_to_head(database_url: str) -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")


def migrate_to_base(database_url: str) -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.downgrade(config, "base")


async def drop_test_role(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            role_exists = (
                await connection.execute(
                    text("SELECT 1 FROM pg_roles WHERE rolname = :role_name"),
                    {"role_name": APP_ROLE},
                )
            ).scalar_one_or_none()
            if role_exists is not None:
                await connection.execute(text(f"DROP OWNED BY {APP_ROLE}"))
                await connection.execute(text(f"DROP ROLE {APP_ROLE}"))
    finally:
        await engine.dispose()


def reset_test_database(database_url: str) -> None:
    asyncio.run(drop_test_role(database_url))
    migrate_to_base(database_url)


async def verify_rls(database_url: str) -> None:
    admin_engine = create_async_engine(database_url)
    app_url = make_url(database_url).set(
        username=APP_ROLE,
        password=APP_PASSWORD,
    )

    try:
        async with admin_engine.begin() as connection:
            await connection.execute(
                text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}'")
            )
            await connection.execute(
                text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
            )
            await connection.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                    f"IN SCHEMA public TO {APP_ROLE}"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO tenant (id, code, name) VALUES "
                    "(:tenant_a, 'tenant-a', 'Tenant A'), "
                    "(:tenant_b, 'tenant-b', 'Tenant B')"
                ),
                {"tenant_a": TENANT_A, "tenant_b": TENANT_B},
            )
            await connection.execute(
                text(
                    "INSERT INTO app_user "
                    "(id, identity_issuer, external_subject, display_name) "
                    "VALUES (:user_id, 'https://issuer.test', 'subject-a', 'User A')"
                ),
                {"user_id": USER_A},
            )

        app_engine = create_async_engine(app_url)
        try:
            async with app_engine.begin() as connection:
                await connection.execute(
                    text(
                        "SELECT set_config('app.current_tenant_id', :tenant_id, true)"
                    ),
                    {"tenant_id": TENANT_A},
                )
                await connection.execute(
                    text(
                        "INSERT INTO tenant_member (id, tenant_id, user_id) "
                        "VALUES (:id, :tenant_id, :user_id)"
                    ),
                    {"id": MEMBER_A, "tenant_id": TENANT_A, "user_id": USER_A},
                )

            async with app_engine.begin() as connection:
                await connection.execute(
                    text(
                        "SELECT set_config('app.current_tenant_id', :tenant_id, true)"
                    ),
                    {"tenant_id": TENANT_B},
                )
                visible_members = (
                    await connection.execute(text("SELECT id FROM tenant_member"))
                ).scalars()
                assert list(visible_members) == []

            with pytest.raises(DBAPIError):
                async with app_engine.begin() as connection:
                    await connection.execute(
                        text(
                            "SELECT set_config"
                            "('app.current_tenant_id', :tenant_id, true)"
                        ),
                        {"tenant_id": TENANT_B},
                    )
                    await connection.execute(
                        text(
                            "INSERT INTO tenant_member (tenant_id, user_id) "
                            "VALUES (:tenant_id, :user_id)"
                        ),
                        {"tenant_id": TENANT_A, "user_id": USER_A},
                    )
        finally:
            await app_engine.dispose()

        async with admin_engine.connect() as connection:
            policies = (
                await connection.execute(
                    text(
                        "SELECT tablename FROM pg_policies "
                        "WHERE policyname = 'tenant_isolation' ORDER BY tablename"
                    )
                )
            ).scalars()
            assert list(policies) == ["role", "role_binding", "tenant_member"]
    finally:
        await admin_engine.dispose()


def test_postgresql_16_migration_and_tenant_rls() -> None:
    database_url = require_database_url()
    reset_test_database(database_url)
    try:
        migrate_to_head(database_url)
        asyncio.run(verify_rls(database_url))
    finally:
        reset_test_database(database_url)

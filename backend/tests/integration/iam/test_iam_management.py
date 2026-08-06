"""Real PostgreSQL IAM management, concurrency and audit verification."""

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

from packages.application.public import IamManagementService, RequestMetadata
from packages.contracts.generated.resources_models import (
    MemberCreateRequest,
    MemberUpdateRequest,
    RoleCreateRequest,
    RoleUpdateRequest,
    TenantCreateRequest,
)
from packages.contracts.public import AuthenticatedPrincipal, PlatformError
from packages.infrastructure.database.public import (
    SqlAlchemyIamPersistence,
    create_session_factory,
)

BACKEND_ROOT = Path(__file__).resolve().parents[3]
DATABASE_URL_ENV = "AP_TEST_DATABASE_URL"
TENANT_A = "11111111-1111-4111-8111-111111111111"
TENANT_B = "22222222-2222-4222-8222-222222222222"
ADMIN_USER = "33333333-3333-4333-8333-333333333333"
ADMIN_MEMBER = "44444444-4444-4444-8444-444444444444"
ADMIN_ROLE = "55555555-5555-4555-8555-555555555555"
TENANT_B_ROLE = "66666666-6666-4666-8666-666666666666"
TENANT_B_USER = "77777777-7777-4777-8777-777777777777"
TENANT_B_MEMBER = "88888888-8888-4888-8888-888888888888"
AUTH_TIME = datetime(2026, 8, 6, 2, 3, 4, tzinfo=UTC)
METADATA = RequestMetadata(request_id="req-iam-integration", trace_id="trace-iam")
ADMIN_PERMISSIONS = (
    "member:create",
    "member:delete",
    "member:list",
    "member:read",
    "member:update",
    "role:create",
    "role:delete",
    "role:list",
    "role:read",
    "role:update",
)


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


def principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        identity_issuer="https://mock.agent-platform.test/",
        external_subject="mock-platform-admin",
        display_name="Mock Platform Admin",
        email="mock-admin@example.test",
        platform_roles=frozenset({"platform_admin"}),
        active_tenant_id=TENANT_A,
        membership_version=1,
        auth_time=AUTH_TIME,
    )


async def seed_iam(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
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
                    "(id, identity_issuer, external_subject, display_name, email) "
                    "VALUES (:user_id, 'https://mock.agent-platform.test/', "
                    "'mock-platform-admin', 'Mock Platform Admin', "
                    "'mock-admin@example.test')"
                ),
                {"user_id": ADMIN_USER},
            )
            await connection.execute(
                text(
                    "INSERT INTO app_user "
                    "(id, identity_issuer, external_subject, display_name, email) "
                    "VALUES (:user_id, 'https://mock.agent-platform.test/', "
                    "'tenant-b-user', 'Tenant B User', 'tenant-b@example.test')"
                ),
                {"user_id": TENANT_B_USER},
            )
            await connection.execute(
                text(
                    "INSERT INTO tenant_member "
                    "(id, tenant_id, user_id, display_name, email) "
                    "VALUES (:member_id, :tenant_id, :user_id, "
                    "'Mock Platform Admin', 'mock-admin@example.test')"
                ),
                {
                    "member_id": ADMIN_MEMBER,
                    "tenant_id": TENANT_A,
                    "user_id": ADMIN_USER,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO role (id, tenant_id, code, name, built_in) "
                    "VALUES (:role_id, :tenant_id, 'tenant_admin', "
                    "'Tenant Admin', true)"
                ),
                {"role_id": ADMIN_ROLE, "tenant_id": TENANT_A},
            )
            for permission in ADMIN_PERMISSIONS:
                resource_type, action = permission.split(":", maxsplit=1)
                await connection.execute(
                    text(
                        "INSERT INTO role_permission "
                        "(tenant_id, role_id, resource_type, action) "
                        "VALUES (:tenant_id, :role_id, :resource_type, :action)"
                    ),
                    {
                        "tenant_id": TENANT_A,
                        "role_id": ADMIN_ROLE,
                        "resource_type": resource_type,
                        "action": action,
                    },
                )
            await connection.execute(
                text(
                    "INSERT INTO role_binding "
                    "(tenant_id, subject_type, subject_id, role_id) "
                    "VALUES (:tenant_id, 'user', :user_id, :role_id)"
                ),
                {
                    "tenant_id": TENANT_A,
                    "user_id": ADMIN_USER,
                    "role_id": ADMIN_ROLE,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO role (id, tenant_id, code, name) "
                    "VALUES (:role_id, :tenant_id, 'tenant_b_role', 'Tenant B Role')"
                ),
                {"role_id": TENANT_B_ROLE, "tenant_id": TENANT_B},
            )
            await connection.execute(
                text(
                    "INSERT INTO tenant_member "
                    "(id, tenant_id, user_id, display_name, email) "
                    "VALUES (:member_id, :tenant_id, :user_id, "
                    "'Tenant B User', 'tenant-b@example.test')"
                ),
                {
                    "member_id": TENANT_B_MEMBER,
                    "tenant_id": TENANT_B,
                    "user_id": TENANT_B_USER,
                },
            )
    finally:
        await engine.dispose()


async def verify_management(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        service = IamManagementService(
            SqlAlchemyIamPersistence(create_session_factory(engine))
        )
        authenticated = principal()

        tenant = await service.create_tenant(
            authenticated,
            request=TenantCreateRequest(code="tenant-c", name="Tenant C"),
            idempotency_key="tenant-create-c",
            metadata=METADATA,
        )
        tenant_replay = await service.create_tenant(
            authenticated,
            request=TenantCreateRequest(code="tenant-c", name="Tenant C"),
            idempotency_key="tenant-create-c",
            metadata=METADATA,
        )
        assert tenant_replay == tenant
        with pytest.raises(PlatformError) as reused:
            await service.create_tenant(
                authenticated,
                request=TenantCreateRequest(code="tenant-d", name="Tenant D"),
                idempotency_key="tenant-create-c",
                metadata=METADATA,
            )
        assert reused.value.code == "IDEMPOTENCY_KEY_REUSED"

        role, role_etag = await service.create_role(
            authenticated,
            request=RoleCreateRequest(
                code="support_operator",
                name="Support Operator",
                description="Tenant-local support role",
                permissions=["member:read"],
            ),
            idempotency_key="role-create-support",
            metadata=METADATA,
        )
        role_replay = await service.create_role(
            authenticated,
            request=RoleCreateRequest(
                code="support_operator",
                name="Support Operator",
                description="Tenant-local support role",
                permissions=["member:read"],
            ),
            idempotency_key="role-create-support",
            metadata=METADATA,
        )
        assert role_replay == (role, role_etag)

        member, _ = await service.create_member(
            authenticated,
            request=MemberCreateRequest(
                external_subject="support-user",
                display_name="Support User",
                email="support@example.test",
                role_ids=[role.id],
            ),
            idempotency_key="member-create-support",
            metadata=METADATA,
        )
        assert member.membership_version == 1

        updated_role, updated_etag = await service.update_role(
            authenticated,
            role_id=role.id,
            if_match=role_etag,
            request=RoleUpdateRequest(
                name="Support Operator",
                description="Expanded support role",
                permissions=["member:list", "member:read"],
                status="ACTIVE",
            ),
            metadata=METADATA,
        )
        assert updated_role.resource_version == 2
        assert updated_etag == '"rv:2"'
        with pytest.raises(PlatformError) as stale_etag:
            await service.update_role(
                authenticated,
                role_id=role.id,
                if_match=role_etag,
                request=RoleUpdateRequest(
                    name="Stale update",
                    description=None,
                    permissions=None,
                    status=None,
                ),
                metadata=METADATA,
            )
        assert stale_etag.value.code == "RESOURCE_VERSION_CONFLICT"

        refreshed_member, _ = await service.get_member(
            authenticated, member.id, METADATA
        )
        assert refreshed_member.membership_version == 2

        with pytest.raises(PlatformError) as cross_tenant:
            await service.get_role(authenticated, TENANT_B_ROLE, METADATA)
        assert cross_tenant.value.code == "RESOURCE_NOT_FOUND"
        with pytest.raises(PlatformError) as cross_tenant_role_write:
            await service.update_role(
                authenticated,
                role_id=TENANT_B_ROLE,
                if_match='"rv:1"',
                request=RoleUpdateRequest(
                    name="Forbidden",
                    description=None,
                    permissions=None,
                    status=None,
                ),
                metadata=METADATA,
            )
        assert cross_tenant_role_write.value.code == "RESOURCE_NOT_FOUND"
        with pytest.raises(PlatformError) as cross_tenant_member_read:
            await service.get_member(authenticated, TENANT_B_MEMBER, METADATA)
        assert cross_tenant_member_read.value.code == "RESOURCE_NOT_FOUND"
        with pytest.raises(PlatformError) as cross_tenant_member_write:
            await service.update_member(
                authenticated,
                member_id=TENANT_B_MEMBER,
                if_match='"rv:1"',
                request=MemberUpdateRequest(
                    display_name="Forbidden",
                    role_ids=None,
                    status=None,
                ),
                metadata=METADATA,
            )
        assert cross_tenant_member_write.value.code == "RESOURCE_NOT_FOUND"

        accepted = await service.delete_role(
            authenticated,
            role_id=role.id,
            if_match=updated_etag,
            idempotency_key="role-delete-support",
            metadata=METADATA,
        )
        replayed = await service.delete_role(
            authenticated,
            role_id=role.id,
            if_match=updated_etag,
            idempotency_key="role-delete-support",
            metadata=METADATA,
        )
        assert replayed == accepted
        operation = await service.get_operation(
            authenticated, accepted.operation_id, METADATA
        )
        assert operation.status == "SUCCEEDED"
        assert operation.resource_type == "role"

        async with engine.connect() as connection:
            membership_version = await connection.scalar(
                text(
                    "SELECT membership_version FROM tenant_member "
                    "WHERE id = :member_id"
                ),
                {"member_id": member.id},
            )
            assert membership_version == 3
            actions = list(
                (
                    await connection.execute(
                        text(
                            "SELECT action FROM audit_log "
                            "WHERE request_id = :request_id ORDER BY created_at, id"
                        ),
                        {"request_id": METADATA.request_id},
                    )
                ).scalars()
            )
            assert "role.bind" in actions
            assert "role.unbind" in actions
            assert "security.cross_tenant_denied" in actions
            assert actions.count("security.cross_tenant_denied") == 4
            assert actions.count("resource.create") >= 3
    finally:
        await engine.dispose()


def test_postgresql_iam_management_idempotency_etag_rbac_and_audit() -> None:
    database_url = require_database_url()
    migrate(database_url, "base")
    try:
        migrate(database_url, "head")
        asyncio.run(seed_iam(database_url))
        asyncio.run(verify_management(database_url))
    finally:
        migrate(database_url, "base")

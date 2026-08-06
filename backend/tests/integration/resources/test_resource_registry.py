"""Real PostgreSQL resource registry, RLS, CAS and idempotency verification."""

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from packages.application.public import RequestMetadata, canonical_request_hash
from packages.contracts.generated.resource_content import ResourceContentPrompt
from packages.contracts.generated.resources_models import (
    ActionRequest,
    ResourceCopyRequest,
    ResourceCreateRequest,
    ResourcePublishRequest,
    ResourceRollbackRequest,
    ResourceUpdateRequest,
)
from packages.contracts.public import PlatformError, SubjectType, TenantContext
from packages.infrastructure.database.public import (
    SqlAlchemyResourceRegistry,
    create_session_factory,
)

BACKEND_ROOT = Path(__file__).resolve().parents[3]
DATABASE_URL_ENV = "AP_TEST_DATABASE_URL"
APP_ROLE = "agent_platform_resource_test"
APP_PASSWORD = "resource-test-only"
TENANT_A = "11111111-1111-4111-8111-111111111111"
TENANT_B = "22222222-2222-4222-8222-222222222222"
ACTOR = UUID("33333333-3333-4333-8333-333333333333")
PERMISSION_TENANT = UUID("44444444-4444-4444-8444-444444444444")
METADATA = RequestMetadata(
    request_id="req-resource-integration", trace_id="trace-resource-integration"
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


def context(tenant_id: str) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        subject_type=SubjectType.USER,
        subject_id=str(ACTOR),
        membership_version=1,
        auth_time=datetime(2026, 8, 6, tzinfo=UTC),
        request_id=METADATA.request_id,
        trace_id=METADATA.trace_id,
    )


def prompt_request(
    *, code: str = "welcome_prompt", template: str = "Hello {{ name }}"
) -> ResourceCreateRequest:
    return ResourceCreateRequest(
        code=code,
        name="Welcome Prompt",
        description=None,
        content_schema_version="1.0",
        content=ResourceContentPrompt(
            resource_type="prompt",
            template=template,
            variables=[],
            language="en",
            compiler_policy_version="1",
        ),
    )


async def drop_test_role(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role_name"),
                {"role_name": APP_ROLE},
            )
            if exists is not None:
                await connection.execute(text(f"DROP OWNED BY {APP_ROLE}"))
                await connection.execute(text(f"DROP ROLE {APP_ROLE}"))
    finally:
        await engine.dispose()


async def seed_tenant_admin_role(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenant (id, code, name) VALUES "
                    "(:tenant_id, 'permission-tenant', 'Permission Tenant')"
                ),
                {"tenant_id": PERMISSION_TENANT},
            )
            await connection.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
                {"tenant_id": str(PERMISSION_TENANT)},
            )
            await connection.execute(
                text(
                    "INSERT INTO role "
                    "(tenant_id, code, name, built_in) VALUES "
                    "(:tenant_id, 'tenant_admin', 'Tenant Admin', true)"
                ),
                {"tenant_id": PERMISSION_TENANT},
            )
    finally:
        await engine.dispose()


async def verify_prompt_permission_backfill(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
                {"tenant_id": str(PERMISSION_TENANT)},
            )
            actions = set(
                (
                    await connection.execute(
                        text(
                            "SELECT action FROM role_permission "
                            "WHERE tenant_id = :tenant_id "
                            "AND resource_type = 'prompt'"
                        ),
                        {"tenant_id": PERMISSION_TENANT},
                    )
                ).scalars()
            )
            assert actions == {
                "create",
                "read",
                "list",
                "update",
                "delete",
                "publish",
                "rollback",
                "disable",
            }
    finally:
        await engine.dispose()


async def verify_resource_registry(database_url: str) -> None:
    admin_engine = create_async_engine(database_url)
    app_url = make_url(database_url).set(username=APP_ROLE, password=APP_PASSWORD)
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
                    "(id, identity_issuer, external_subject, display_name) VALUES "
                    "(:actor_id, 'https://issuer.test', 'resource-owner', "
                    "'Resource Owner')"
                ),
                {"actor_id": ACTOR},
            )

        app_engine = create_async_engine(app_url)
        try:
            registry = SqlAlchemyResourceRegistry(create_session_factory(app_engine))
            request = prompt_request()
            request_hash = canonical_request_hash("prompt.create", request)
            created = await registry.create_definition(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="prompt",
                request=request,
                idempotency_key="prompt-create-welcome",
                request_hash=request_hash,
                metadata=METADATA,
            )
            assert created.value is not None
            definition = created.value
            assert definition.resource_version == 1
            replay = await registry.create_definition(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="prompt",
                request=request,
                idempotency_key="prompt-create-welcome",
                request_hash=request_hash,
                metadata=METADATA,
            )
            assert replay.replay is not None
            assert replay.replay.response_body["id"] == str(definition.id)

            with pytest.raises(PlatformError) as reused:
                changed = prompt_request(template="Different request")
                await registry.create_definition(
                    context(TENANT_A),
                    actor_id=ACTOR,
                    resource_type="prompt",
                    request=changed,
                    idempotency_key="prompt-create-welcome",
                    request_hash=canonical_request_hash("prompt.create", changed),
                    metadata=METADATA,
                )
            assert reused.value.code == "IDEMPOTENCY_KEY_REUSED"

            tenant_b = await registry.create_definition(
                context(TENANT_B),
                actor_id=ACTOR,
                resource_type="prompt",
                request=request,
                idempotency_key="prompt-create-welcome-b",
                request_hash=request_hash,
                metadata=METADATA,
            )
            assert tenant_b.value is not None
            assert tenant_b.value.code == definition.code
            assert (
                await registry.get_definition(
                    context(TENANT_B),
                    resource_type="prompt",
                    resource_id=definition.id,
                )
                is None
            )

            updated = await registry.update_definition(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="prompt",
                resource_id=definition.id,
                expected_version=1,
                request=ResourceUpdateRequest(
                    name="Welcome Prompt V2",
                    description=None,
                    visibility=None,
                    content_schema_version=None,
                    content=None,
                ),
                metadata=METADATA,
            )
            assert updated is not None
            assert updated.resource_version == 2
            with pytest.raises(PlatformError) as stale:
                await registry.update_definition(
                    context(TENANT_A),
                    actor_id=ACTOR,
                    resource_type="prompt",
                    resource_id=definition.id,
                    expected_version=1,
                    request=ResourceUpdateRequest(
                        name="Stale",
                        description=None,
                        visibility=None,
                        content_schema_version=None,
                        content=None,
                    ),
                    metadata=METADATA,
                )
            assert stale.value.code == "RESOURCE_VERSION_CONFLICT"

            publish_request = ResourcePublishRequest(
                expected_resource_version=2, release_note="Initial version"
            )
            published = await registry.publish_version(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="prompt",
                resource_id=definition.id,
                request=publish_request,
                idempotency_key="prompt-publish-v1",
                request_hash=canonical_request_hash(
                    "prompt.publish", publish_request, extra={"id": str(definition.id)}
                ),
                metadata=METADATA,
            )
            assert published.value is not None
            version_one = published.value
            assert version_one.version_no == 1
            assert version_one.content_hash.startswith("sha256:")
            published_replay = await registry.publish_version(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="prompt",
                resource_id=definition.id,
                request=publish_request,
                idempotency_key="prompt-publish-v1",
                request_hash=canonical_request_hash(
                    "prompt.publish", publish_request, extra={"id": str(definition.id)}
                ),
                metadata=METADATA,
            )
            assert published_replay.replay is not None
            assert published_replay.replay.response_body["id"] == str(version_one.id)

            revised_content = ResourceContentPrompt(
                resource_type="prompt",
                template="Hi {{ name }}",
                variables=[],
                language="en",
                compiler_policy_version="1",
            )
            revised = await registry.update_definition(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="prompt",
                resource_id=definition.id,
                expected_version=3,
                request=ResourceUpdateRequest(
                    name=None,
                    description=None,
                    visibility=None,
                    content_schema_version=None,
                    content=revised_content,
                ),
                metadata=METADATA,
            )
            assert revised is not None
            assert revised.resource_version == 4
            assert isinstance(version_one.content, ResourceContentPrompt)
            assert version_one.content.template == "Hello {{ name }}"

            second_publish = ResourcePublishRequest(
                expected_resource_version=4, release_note="Revised greeting"
            )
            version_two = await registry.publish_version(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="prompt",
                resource_id=definition.id,
                request=second_publish,
                idempotency_key="prompt-publish-v2",
                request_hash=canonical_request_hash(
                    "prompt.publish", second_publish, extra={"id": str(definition.id)}
                ),
                metadata=METADATA,
            )
            assert version_two.value is not None
            assert version_two.value.version_no == 2

            duplicate_publish = ResourcePublishRequest(
                expected_resource_version=5, release_note="Duplicate content"
            )
            with pytest.raises(PlatformError) as duplicate:
                await registry.publish_version(
                    context(TENANT_A),
                    actor_id=ACTOR,
                    resource_type="prompt",
                    resource_id=definition.id,
                    request=duplicate_publish,
                    idempotency_key="prompt-publish-duplicate",
                    request_hash=canonical_request_hash(
                        "prompt.publish",
                        duplicate_publish,
                        extra={"id": str(definition.id)},
                    ),
                    metadata=METADATA,
                )
            assert duplicate.value.code == "RESOURCE_STATE_CONFLICT"

            versions, cursor = await registry.list_versions(
                context(TENANT_A),
                resource_type="prompt",
                resource_id=definition.id,
                limit=20,
                cursor=None,
            )
            assert [version.version_no for version in versions] == [2, 1]
            assert cursor is None
            assert isinstance(versions[1].content, ResourceContentPrompt)
            assert versions[1].content.template == "Hello {{ name }}"

            rollback_request = ResourceRollbackRequest(
                version_id=str(version_one.id),
                expected_resource_version=5,
                release_note="Restore initial wording",
            )
            rolled_back = await registry.rollback_version(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="prompt",
                resource_id=definition.id,
                source_version_id=version_one.id,
                request=rollback_request,
                idempotency_key="prompt-rollback-v1",
                request_hash=canonical_request_hash(
                    "prompt.rollback",
                    rollback_request,
                    extra={"resource_id": str(definition.id)},
                ),
                metadata=METADATA,
            )
            assert rolled_back is not None
            assert rolled_back.value is not None
            assert rolled_back.value.version_no == 3
            assert rolled_back.value.content_hash == version_one.content_hash
            assert isinstance(rolled_back.value.content, ResourceContentPrompt)
            assert rolled_back.value.content.template == "Hello {{ name }}"

            disabled = await registry.set_definition_status(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="prompt",
                resource_id=definition.id,
                expected_version=6,
                enabled=False,
                request=ActionRequest(reason="Maintenance"),
                idempotency_key="prompt-disable-1",
                request_hash=canonical_request_hash(
                    "prompt.disable", extra={"resource_id": str(definition.id)}
                ),
                metadata=METADATA,
            )
            assert disabled is not None
            assert disabled.value is not None
            assert disabled.value.status == "DISABLED"
            enabled = await registry.set_definition_status(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="prompt",
                resource_id=definition.id,
                expected_version=7,
                enabled=True,
                request=None,
                idempotency_key="prompt-enable-1",
                request_hash=canonical_request_hash(
                    "prompt.enable", extra={"resource_id": str(definition.id)}
                ),
                metadata=METADATA,
            )
            assert enabled is not None
            assert enabled.value is not None
            assert enabled.value.status == "ACTIVE"

            copy_request = ResourceCopyRequest(
                code="welcome_prompt_copy", name="Welcome Prompt Copy"
            )
            copied = await registry.copy_definition(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="prompt",
                resource_id=definition.id,
                request=copy_request,
                idempotency_key="prompt-copy-1",
                request_hash=canonical_request_hash(
                    "prompt.copy",
                    copy_request,
                    extra={"resource_id": str(definition.id)},
                ),
                metadata=METADATA,
            )
            assert copied is not None
            assert copied.value is not None
            assert copied.value.code == "welcome_prompt_copy"
            deleted = await registry.delete_definition(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="prompt",
                resource_id=copied.value.id,
                expected_version=1,
                idempotency_key="prompt-delete-copy",
                request_hash=canonical_request_hash(
                    "prompt.delete", extra={"resource_id": str(copied.value.id)}
                ),
                metadata=METADATA,
            )
            assert deleted is not None
            assert deleted.value is not None
            assert deleted.value.status == "SUCCEEDED"
            assert (
                await registry.get_definition(
                    context(TENANT_A),
                    resource_type="prompt",
                    resource_id=copied.value.id,
                )
                is None
            )
        finally:
            await app_engine.dispose()

        async with admin_engine.connect() as connection:
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
            assert actions.count("resource.create") == 2
            assert actions.count("resource.update") == 2
            assert actions.count("resource.publish") == 2
            assert actions.count("resource.rollback") == 1
            assert actions.count("resource.disable") == 1
            assert actions.count("resource.enable") == 1
            assert actions.count("resource.copy") == 1
            assert actions.count("resource.delete") == 1
            audit_payload = str(
                (
                    await connection.execute(
                        text(
                            "SELECT jsonb_agg(metadata_json) FROM audit_log "
                            "WHERE request_id = :request_id"
                        ),
                        {"request_id": METADATA.request_id},
                    )
                ).scalar_one()
            )
            assert "Hi {{ name }}" not in audit_payload
            assert "Maintenance" not in audit_payload
    finally:
        await admin_engine.dispose()


def test_resource_registry_postgresql_integration() -> None:
    database_url = require_database_url()
    asyncio.run(drop_test_role(database_url))
    migrate(database_url, "base")
    migrate(database_url, "0004_resource_registry")
    asyncio.run(seed_tenant_admin_role(database_url))
    migrate(database_url, "head")
    try:
        asyncio.run(verify_prompt_permission_backfill(database_url))
        asyncio.run(verify_resource_registry(database_url))
    finally:
        asyncio.run(drop_test_role(database_url))
        migrate(database_url, "base")

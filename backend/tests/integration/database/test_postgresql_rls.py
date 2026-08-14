"""Real PostgreSQL migration and RLS isolation verification."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from packages.application.artifacts import (
    ARTIFACT_DELETE_REQUESTED_EVENT,
    ARTIFACT_DOWNLOADS_REVOKED_EVENT,
    ARTIFACT_SCAN_REQUESTED_EVENT,
    ArtifactDeleteProcessor,
)
from packages.application.metadata import RequestMetadata
from packages.application.model_gateway import BudgetPermit
from packages.application.policy import ArtifactStoragePolicy, TenantStoragePolicy
from packages.contracts.generated.core_models import ArtifactUploadCreateRequest
from packages.contracts.generated.resources_models import StoragePolicyCreateRequest
from packages.contracts.model_gateway import (
    ImmutableReference,
    ModelGatewayRequest,
    ModelUsage,
)
from packages.contracts.public import PlatformError, SubjectType, TenantContext
from packages.domain.model_gateway import (
    ModelBinding,
    ModelRoute,
    ModelUsageRecord,
    ProviderError,
)
from packages.domain.public import ArtifactRecord
from packages.infrastructure.database.audit_security import audit_change_digest
from packages.infrastructure.database.models import (
    ArtifactModel,
    AuditLogModel,
    IdempotencyRecordModel,
    OperationRecordModel,
    OutboxEventModel,
    PriceCatalogRateModel,
    PriceCatalogVersionModel,
)
from packages.infrastructure.database.outbox import SqlAlchemyOutboxStore
from packages.infrastructure.database.public import (
    SqlAlchemyArtifactStore,
    SqlAlchemyAuditQueryStore,
    SqlAlchemyStoragePolicyStore,
    SqlAlchemyTenantContextSource,
    create_session_factory,
)
from packages.infrastructure.database.uow import PlatformUnitOfWork, TenantUnitOfWork
from packages.infrastructure.model_gateway import (
    SqlAlchemyModelBudgetGuard,
    SqlAlchemyModelGatewayStore,
    SqlAlchemyModelRateLimiter,
    SqlAlchemyPriceCatalogReader,
)

BACKEND_ROOT = Path(__file__).resolve().parents[3]
DATABASE_URL_ENV = "AP_TEST_DATABASE_URL"
APP_ROLE = "agent_platform_rls_test"
APP_PASSWORD = "rls-test-only"
TENANT_A = "11111111-1111-4111-8111-111111111111"
TENANT_B = "22222222-2222-4222-8222-222222222222"
USER_A = "33333333-3333-4333-8333-333333333333"
MEMBER_A = "44444444-4444-4444-8444-444444444444"
OUTBOX_A = "55555555-5555-4555-8555-555555555555"
PROBE_A = "66666666-6666-4666-8666-666666666666"
MODEL_USAGE_A = "77777777-7777-4777-8777-777777777777"
PRICE_CATALOG_A = UUID("77777777-7777-4777-8777-777777777778")
PRICE_RATE_A = UUID("77777777-7777-4777-8777-777777777779")
BUDGET_POLICY_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaab1"
BUDGET_POLICY_VERSION_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaab2"
HASH = "sha256:" + "a" * 64
ARTIFACT_A = UUID("88888888-8888-4888-8888-888888888888")
ARTIFACT_FAILED = UUID("99999999-9999-4999-8999-999999999999")
ARTIFACT_CONCURRENT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaac1")
ARTIFACT_CONCURRENT_OTHER = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaac2")
ARTIFACT_EXPIRED_UPLOAD = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaac3")
ARTIFACT_REPLACEMENT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaac5")
AUDIT_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1")
AUDIT_A_OLDER = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2")
AUDIT_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1")
AUDIT_PLATFORM = UUID("cccccccc-cccc-4ccc-8ccc-ccccccccccc1")
AUDIT_RUN_A = UUID("dddddddd-dddd-4ddd-8ddd-ddddddddddd1")
AUDIT_RUN_B = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee1")


class ArtifactDeletionRecorder:
    def __init__(self) -> None:
        self.deleted: list[UUID] = []

    async def revoke_download_access(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> None:
        return None

    async def delete_artifact_objects(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> None:
        self.deleted.append(artifact.id)


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


async def verify_audit_controls(
    app_engine: AsyncEngine, tenant_context: TenantContext
) -> None:
    factory = create_session_factory(app_engine)
    tenant_b_context = tenant_context.model_copy(update={"tenant_id": TENANT_B})
    older_at = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
    newer_at = datetime(2026, 8, 8, 11, 0, tzinfo=UTC)

    async with TenantUnitOfWork(factory, tenant_context) as unit:
        unit.session.add_all(
            [
                AuditLogModel(
                    id=AUDIT_A_OLDER,
                    tenant_id=UUID(TENANT_A),
                    actor_type="service",
                    actor_id=UUID(USER_A),
                    action="tool.execute",
                    resource_type="tool",
                    resource_id=AUDIT_RUN_A,
                    result="SUCCESS",
                    reason_codes=[],
                    request_id="req-audit-older",
                    trace_id="trace-audit-older",
                    metadata_json={
                        "run_id": str(AUDIT_RUN_A),
                        "secret_ref": "secret://tenant/a/tool",
                        "ticket_nonce": "plain-nonce",
                        "arguments": {"path": "/production"},
                        "content_hash": HASH,
                        "tool_schema_hash": HASH,
                        "token_count": 12,
                    },
                    created_at=older_at,
                ),
                AuditLogModel(
                    id=AUDIT_A,
                    tenant_id=UUID(TENANT_A),
                    actor_type="service",
                    actor_id=UUID(USER_A),
                    action="run.cancel",
                    resource_type="run",
                    resource_id=AUDIT_RUN_B,
                    result="DENIED",
                    reason_codes=["POLICY_DENIED"],
                    request_id="req-audit-newer",
                    trace_id="trace-audit-newer",
                    metadata_json={"policy_version": "policy-v1"},
                    created_at=newer_at,
                ),
            ]
        )

    async with TenantUnitOfWork(factory, tenant_b_context) as unit:
        unit.session.add(
            AuditLogModel(
                id=AUDIT_B,
                tenant_id=UUID(TENANT_B),
                actor_type="service",
                actor_id=UUID(USER_A),
                action="tenant-b-only",
                resource_type="run",
                resource_id=AUDIT_RUN_A,
                result="SUCCESS",
                reason_codes=[],
                request_id="req-audit-b",
                trace_id="trace-audit-b",
                metadata_json={},
                created_at=newer_at,
            )
        )

    async with PlatformUnitOfWork(factory) as unit:
        unit.session.add(
            AuditLogModel(
                id=AUDIT_PLATFORM,
                tenant_id=None,
                actor_type="service",
                actor_id=UUID(USER_A),
                action="platform.maintenance",
                resource_type="platform",
                resource_id=None,
                result="SUCCESS",
                reason_codes=[],
                request_id="req-audit-platform",
                trace_id="trace-audit-platform",
                metadata_json={},
                created_at=newer_at,
            )
        )

    store = SqlAlchemyAuditQueryStore(factory)
    first_page, next_cursor = await store.list_audit_logs(
        tenant_context,
        action=None,
        resource_type=None,
        actor_id=None,
        run_id=None,
        occurred_from=None,
        occurred_to=None,
        limit=1,
        cursor=None,
    )
    assert [record.event_id for record in first_page] == [AUDIT_A]
    assert next_cursor is not None
    second_page, terminal_cursor = await store.list_audit_logs(
        tenant_context,
        action=None,
        resource_type=None,
        actor_id=None,
        run_id=None,
        occurred_from=None,
        occurred_to=None,
        limit=1,
        cursor=next_cursor,
    )
    assert [record.event_id for record in second_page] == [AUDIT_A_OLDER]
    assert terminal_cursor is None

    filtered, _ = await store.list_audit_logs(
        tenant_context,
        action="tool.execute",
        resource_type="tool",
        actor_id=UUID(USER_A),
        run_id=AUDIT_RUN_A,
        occurred_from=older_at,
        occurred_to=older_at,
        limit=20,
        cursor=None,
    )
    assert [record.event_id for record in filtered] == [AUDIT_A_OLDER]

    async with TenantUnitOfWork(factory, tenant_context, read_only=True) as unit:
        persisted = (
            await unit.session.scalars(
                select(AuditLogModel).where(AuditLogModel.id == AUDIT_A_OLDER)
            )
        ).one()
        assert persisted.run_id == AUDIT_RUN_A
        assert persisted.metadata_json["secret_ref"] == "[REDACTED]"
        assert persisted.metadata_json["ticket_nonce"] == "[REDACTED]"
        assert persisted.metadata_json["arguments"] == "[REDACTED]"
        assert persisted.metadata_json["content_hash"] == HASH
        assert persisted.metadata_json["tool_schema_hash"] == HASH
        assert persisted.metadata_json["token_count"] == 12
        assert persisted.change_digest == audit_change_digest(persisted.metadata_json)

    async with PlatformUnitOfWork(factory) as unit:
        visible_ids = set(
            (
                await unit.session.scalars(
                    select(AuditLogModel.id).where(
                        AuditLogModel.id.in_(
                            [AUDIT_A, AUDIT_A_OLDER, AUDIT_B, AUDIT_PLATFORM]
                        )
                    )
                )
            ).all()
        )
        assert visible_ids == {AUDIT_A, AUDIT_A_OLDER, AUDIT_B, AUDIT_PLATFORM}

    with pytest.raises(DBAPIError, match="immutable and retained"):
        async with TenantUnitOfWork(factory, tenant_context) as unit:
            await unit.session.execute(
                text("UPDATE audit_log SET action = action WHERE id = :audit_id"),
                {"audit_id": AUDIT_A},
            )
    with pytest.raises(DBAPIError, match="immutable and retained"):
        async with TenantUnitOfWork(factory, tenant_context) as unit:
            await unit.session.execute(
                text("DELETE FROM audit_log WHERE id = :audit_id"),
                {"audit_id": AUDIT_A},
            )


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
                    "INSERT INTO tenant (id, code, name, status) VALUES "
                    "(:tenant_a, 'tenant-a', 'Tenant A', 'ACTIVE'), "
                    "(:tenant_b, 'tenant-b', 'Tenant B', 'DISABLED')"
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
            worker_contexts = await SqlAlchemyTenantContextSource(
                create_session_factory(app_engine),
                service_subject_id=UUID(USER_A),
                process_name="event-worker",
            ).list_service_contexts(limit=10)
            assert {context.tenant_id for context in worker_contexts} == {
                TENANT_A,
                TENANT_B,
            }
            assert all(
                context.subject_type is SubjectType.SERVICE
                for context in worker_contexts
            )

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
                await connection.execute(
                    text(
                        "INSERT INTO model_usage "
                        "(id, tenant_id, run_id, provider, model, input_tokens, "
                        "output_tokens, reasoning_tokens, cache_read_tokens, "
                        "cache_write_tokens, token_estimated, started_at, finished_at) "
                        "VALUES (:id, :tenant_id, 'run-a', 'openai', 'model-a', "
                        "1, 2, 0, 0, 0, false, :occurred_at, :occurred_at)"
                    ),
                    {
                        "id": MODEL_USAGE_A,
                        "tenant_id": TENANT_A,
                        "occurred_at": datetime(2026, 8, 7, tzinfo=UTC),
                    },
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
                visible_usage = (
                    await connection.execute(text("SELECT id FROM model_usage"))
                ).scalars()
                assert list(visible_usage) == []

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

            async with admin_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO outbox_event "
                        "(id, tenant_id, aggregate_type, aggregate_id, event_type, "
                        "payload_json, payload_schema_version, next_attempt_at) VALUES "
                        "(:id, :tenant_id, 'probe', :probe_id, "
                        "'platform_probe_requested.v1', '{}'::jsonb, 1, :ready_at)"
                    ),
                    {
                        "id": OUTBOX_A,
                        "tenant_id": TENANT_A,
                        "probe_id": PROBE_A,
                        "ready_at": datetime(2026, 8, 6, tzinfo=UTC),
                    },
                )

            outbox_store = SqlAlchemyOutboxStore(
                async_sessionmaker(
                    app_engine,
                    autoflush=False,
                    expire_on_commit=False,
                )
            )
            tenant_context = TenantContext(
                tenant_id=TENANT_A,
                subject_type=SubjectType.SERVICE,
                subject_id=USER_A,
                auth_time=datetime(2026, 8, 6, tzinfo=UTC),
                request_id="req-outbox-postgresql",
                trace_id="trace-outbox-postgresql",
            )
            await verify_audit_controls(app_engine, tenant_context)
            now = datetime(2026, 8, 7, tzinfo=UTC)
            claimed = await outbox_store.claim_ready(
                tenant_context,
                now=now,
                limit=10,
                lease_duration=timedelta(seconds=30),
            )
            assert len(claimed) == 1
            assert claimed[0].attempts == 1
            assert claimed[0].status.value == "PUBLISHING"
            await outbox_store.mark_published(
                tenant_context,
                claimed[0].id,
                now=now,
            )

            policy_factory = create_session_factory(app_engine)
            async with TenantUnitOfWork(policy_factory, tenant_context) as unit:
                unit.session.add(
                    PriceCatalogVersionModel(
                        id=PRICE_CATALOG_A,
                        tenant_id=UUID(TENANT_A),
                        provider="openai",
                        model="model-a",
                        currency="USD",
                        effective_from=datetime(2026, 8, 1, tzinfo=UTC),
                        effective_to=None,
                        source_ref="test://trusted-price-fixture",
                        content_hash="sha256:" + "c" * 64,
                        created_by=UUID(USER_A),
                    )
                )
                await unit.session.flush()
                unit.session.add(
                    PriceCatalogRateModel(
                        id=PRICE_RATE_A,
                        tenant_id=UUID(TENANT_A),
                        catalog_version_id=PRICE_CATALOG_A,
                        dimension="input_tokens",
                        unit_tokens=1000,
                        unit_price=Decimal("0.100000000000"),
                    )
                )
            catalog = await SqlAlchemyPriceCatalogReader(policy_factory).get_catalog(
                tenant_context,
                provider="openai",
                model="model-a",
                occurred_at=datetime(2026, 8, 7, tzinfo=UTC),
            )
            assert catalog is not None
            assert catalog.id == PRICE_CATALOG_A
            assert catalog.rates[0].id == PRICE_RATE_A
            assert (
                await SqlAlchemyPriceCatalogReader(policy_factory).get_catalog(
                    tenant_context.model_copy(update={"tenant_id": TENANT_B}),
                    provider="openai",
                    model="model-a",
                    occurred_at=datetime(2026, 8, 7, tzinfo=UTC),
                )
                is None
            )
            storage_policy_outcome = await SqlAlchemyStoragePolicyStore(
                policy_factory
            ).create_policy(
                tenant_context,
                actor_id=UUID(USER_A),
                request=StoragePolicyCreateRequest.model_validate(
                    {
                        "name": "PostgreSQL storage policy",
                        "limits": {
                            "max_reserved_artifact_bytes": 12,
                            "max_reserved_artifacts": 2,
                        },
                    }
                ),
                limits=TenantStoragePolicy(
                    max_reserved_artifact_bytes=12,
                    max_reserved_artifacts=2,
                ),
                idempotency_key="postgresql-storage-policy-create",
                request_hash="postgresql-storage-policy-create-hash",
                metadata=RequestMetadata(
                    request_id="req-storage-policy-create",
                    trace_id="trace-storage-policy-create",
                ),
            )
            assert storage_policy_outcome.value is not None
            storage_policy = storage_policy_outcome.value
            artifact_store = SqlAlchemyArtifactStore(
                policy_factory,
                storage_policy=ArtifactStoragePolicy(
                    max_reserved_bytes_per_tenant=24,
                    max_reserved_artifacts_per_tenant=2,
                ),
            )
            artifact_now = datetime.now(UTC)
            artifact_request = ArtifactUploadCreateRequest(
                name="postgresql-result.txt",
                size=12,
                content_type="text/plain",
                content_hash=HASH,
            )
            artifact = await artifact_store.create_upload(
                tenant_context,
                artifact_id=ARTIFACT_A,
                owner_user_id=UUID(USER_A),
                request=artifact_request,
                quarantine_object_uri=(
                    f"quarantine://tenant/{TENANT_A}/artifact/{ARTIFACT_A}/source"
                ),
                upload_expires_at=artifact_now + timedelta(minutes=15),
                expires_at=artifact_now + timedelta(days=30),
                idempotency_key="postgresql-artifact-create",
                request_hash="artifact-create-hash",
                metadata=RequestMetadata(
                    request_id="req-artifact-create",
                    trace_id="trace-artifact-create",
                ),
            )
            assert artifact.status == "UPLOADING"
            async with TenantUnitOfWork(
                policy_factory, tenant_context, read_only=True
            ) as unit:
                creation_audit = await unit.session.scalar(
                    select(AuditLogModel).where(
                        AuditLogModel.tenant_id == UUID(TENANT_A),
                        AuditLogModel.action == "artifact.upload.create",
                        AuditLogModel.resource_id == ARTIFACT_A,
                    )
                )
                assert creation_audit is not None
                assert creation_audit.metadata_json["storage_policy_version_id"] == str(
                    storage_policy.current_version.id
                )
            replayed_artifact = await artifact_store.create_upload(
                tenant_context,
                artifact_id=ARTIFACT_A,
                owner_user_id=UUID(USER_A),
                request=artifact_request,
                quarantine_object_uri=(
                    f"quarantine://tenant/{TENANT_A}/artifact/{ARTIFACT_A}/source"
                ),
                upload_expires_at=artifact_now + timedelta(minutes=15),
                expires_at=artifact_now + timedelta(days=30),
                idempotency_key="postgresql-artifact-create",
                request_hash="artifact-create-hash",
                metadata=RequestMetadata(
                    request_id="req-artifact-create-replay",
                    trace_id="trace-artifact-create-replay",
                ),
            )
            assert replayed_artifact.id == ARTIFACT_A
            with pytest.raises(PlatformError) as reused_artifact_key:
                await artifact_store.create_upload(
                    tenant_context,
                    artifact_id=ARTIFACT_A,
                    owner_user_id=UUID(USER_A),
                    request=artifact_request,
                    quarantine_object_uri=(
                        f"quarantine://tenant/{TENANT_A}/artifact/{ARTIFACT_A}/source"
                    ),
                    upload_expires_at=artifact_now + timedelta(minutes=15),
                    expires_at=artifact_now + timedelta(days=30),
                    idempotency_key="postgresql-artifact-create",
                    request_hash="different-artifact-create-hash",
                    metadata=RequestMetadata(
                        request_id="req-artifact-create-conflict",
                        trace_id="trace-artifact-create-conflict",
                    ),
                )
            assert reused_artifact_key.value.code == "IDEMPOTENCY_KEY_REUSED"
            with pytest.raises(PlatformError) as artifact_capacity:
                await artifact_store.create_upload(
                    tenant_context,
                    artifact_id=ARTIFACT_FAILED,
                    owner_user_id=UUID(USER_A),
                    request=artifact_request.model_copy(update={"size": 13}),
                    quarantine_object_uri=(
                        f"quarantine://tenant/{TENANT_A}/artifact/"
                        f"{ARTIFACT_FAILED}/capacity"
                    ),
                    upload_expires_at=artifact_now + timedelta(minutes=15),
                    expires_at=artifact_now + timedelta(days=30),
                    idempotency_key="postgresql-artifact-capacity-denied",
                    request_hash="artifact-capacity-denied-hash",
                    metadata=RequestMetadata(
                        request_id="req-artifact-capacity-denied",
                        trace_id="trace-artifact-capacity-denied",
                    ),
                )
            assert artifact_capacity.value.code == "RATE_LIMITED"
            assert artifact_capacity.value.details == {
                "scope": "tenant",
                "reason_code": "ARTIFACT_TENANT_STORAGE_BYTES_LIMIT",
            }
            async with TenantUnitOfWork(
                policy_factory, tenant_context, read_only=True
            ) as unit:
                denial_audit = await unit.session.scalar(
                    select(AuditLogModel).where(
                        AuditLogModel.tenant_id == UUID(TENANT_A),
                        AuditLogModel.action == "artifact.upload.admission_deny",
                    )
                )
                assert denial_audit is not None
                assert denial_audit.result == "DENIED"
                assert denial_audit.metadata_json["current"] == 12
                assert denial_audit.metadata_json["requested"] == 13
                assert denial_audit.metadata_json["storage_policy_version_id"] == str(
                    storage_policy.current_version.id
                )
                assert (
                    await unit.session.scalar(
                        select(IdempotencyRecordModel.id).where(
                            IdempotencyRecordModel.tenant_id == UUID(TENANT_A),
                            IdempotencyRecordModel.actor_id == UUID(USER_A),
                            IdempotencyRecordModel.operation_type
                            == "artifact.upload.create",
                            IdempotencyRecordModel.idempotency_key
                            == "postgresql-artifact-capacity-denied",
                        )
                    )
                    is None
                )
            disabled = await SqlAlchemyStoragePolicyStore(
                policy_factory
            ).set_policy_status(
                tenant_context,
                actor_id=UUID(USER_A),
                policy_id=storage_policy.id,
                expected_version=1,
                enabled=False,
                request=None,
                idempotency_key="postgresql-storage-policy-disable",
                request_hash="postgresql-storage-policy-disable-hash",
                metadata=RequestMetadata(
                    request_id="req-storage-policy-disable",
                    trace_id="trace-storage-policy-disable",
                ),
            )
            assert disabled is not None
            assert disabled.value is not None
            assert disabled.value.status == "DISABLED"
            concurrent_store = SqlAlchemyArtifactStore(
                policy_factory,
                storage_policy=ArtifactStoragePolicy(
                    max_reserved_bytes_per_tenant=24,
                    max_reserved_artifacts_per_tenant=3,
                ),
            )
            concurrent_results = await asyncio.gather(
                concurrent_store.create_upload(
                    tenant_context,
                    artifact_id=ARTIFACT_CONCURRENT,
                    owner_user_id=UUID(USER_A),
                    request=artifact_request,
                    quarantine_object_uri=(
                        f"quarantine://tenant/{TENANT_A}/artifact/"
                        f"{ARTIFACT_CONCURRENT}/source"
                    ),
                    upload_expires_at=artifact_now + timedelta(minutes=15),
                    expires_at=artifact_now + timedelta(days=30),
                    idempotency_key="postgresql-artifact-concurrent-a",
                    request_hash="artifact-concurrent-a-hash",
                    metadata=RequestMetadata(
                        request_id="req-artifact-concurrent-a",
                        trace_id="trace-artifact-concurrent-a",
                    ),
                ),
                concurrent_store.create_upload(
                    tenant_context,
                    artifact_id=ARTIFACT_CONCURRENT_OTHER,
                    owner_user_id=UUID(USER_A),
                    request=artifact_request,
                    quarantine_object_uri=(
                        f"quarantine://tenant/{TENANT_A}/artifact/"
                        f"{ARTIFACT_CONCURRENT_OTHER}/source"
                    ),
                    upload_expires_at=artifact_now + timedelta(minutes=15),
                    expires_at=artifact_now + timedelta(days=30),
                    idempotency_key="postgresql-artifact-concurrent-b",
                    request_hash="artifact-concurrent-b-hash",
                    metadata=RequestMetadata(
                        request_id="req-artifact-concurrent-b",
                        trace_id="trace-artifact-concurrent-b",
                    ),
                ),
                return_exceptions=True,
            )
            assert (
                sum(
                    isinstance(result, PlatformError) and result.code == "RATE_LIMITED"
                    for result in concurrent_results
                )
                == 1
            )
            concurrent_artifact = next(
                result
                for result in concurrent_results
                if not isinstance(result, BaseException)
            )
            await concurrent_store.fail_upload(
                tenant_context,
                artifact_id=concurrent_artifact.id,
                owner_user_id=UUID(USER_A),
                code="ARTIFACT_UPLOAD_MISMATCH",
                now=artifact_now + timedelta(seconds=1),
            )
            concurrent_deletion = await concurrent_store.request_delete(
                tenant_context,
                artifact_id=concurrent_artifact.id,
                owner_user_id=UUID(USER_A),
                idempotency_key="postgresql-artifact-concurrent-delete",
                request_hash="artifact-concurrent-delete-hash",
                metadata=RequestMetadata(
                    request_id="req-artifact-concurrent-delete",
                    trace_id="trace-artifact-concurrent-delete",
                ),
                now=artifact_now + timedelta(seconds=2),
            )
            assert concurrent_deletion is not None
            assert concurrent_deletion.value is not None
            await concurrent_store.complete_delete(
                tenant_context,
                artifact_id=concurrent_artifact.id,
                operation_id=concurrent_deletion.value.id,
                now=artifact_now + timedelta(seconds=3),
            )
            concurrent_delete_outbox = SqlAlchemyOutboxStore(
                policy_factory,
                event_types=frozenset({ARTIFACT_DELETE_REQUESTED_EVENT}),
            )
            concurrent_delete_events = await concurrent_delete_outbox.claim_ready(
                tenant_context,
                now=artifact_now + timedelta(seconds=3),
                limit=10,
                lease_duration=timedelta(seconds=30),
            )
            assert len(concurrent_delete_events) == 1
            await concurrent_delete_outbox.mark_published(
                tenant_context,
                concurrent_delete_events[0].id,
                now=artifact_now + timedelta(seconds=3),
            )
            scanning = await artifact_store.mark_scanning(
                tenant_context,
                artifact_id=ARTIFACT_A,
                owner_user_id=UUID(USER_A),
                idempotency_key="postgresql-artifact-complete",
                request_hash="artifact-complete-hash",
                metadata=RequestMetadata(
                    request_id="req-artifact-complete",
                    trace_id="trace-artifact-complete",
                ),
                now=artifact_now,
            )
            assert scanning is not None
            assert scanning.status == "SCANNING"
            replayed_scanning = await artifact_store.mark_scanning(
                tenant_context,
                artifact_id=ARTIFACT_A,
                owner_user_id=UUID(USER_A),
                idempotency_key="postgresql-artifact-complete",
                request_hash="artifact-complete-hash",
                metadata=RequestMetadata(
                    request_id="req-artifact-complete-replay",
                    trace_id="trace-artifact-complete-replay",
                ),
                now=artifact_now,
            )
            assert replayed_scanning is not None
            assert replayed_scanning.status == "SCANNING"
            scan_now = scanning.updated_at
            artifact_outbox = SqlAlchemyOutboxStore(
                policy_factory,
                event_types=frozenset({ARTIFACT_SCAN_REQUESTED_EVENT}),
            )
            scan_events = await artifact_outbox.claim_ready(
                tenant_context,
                now=scan_now,
                limit=10,
                lease_duration=timedelta(seconds=30),
            )
            assert len(scan_events) == 1
            assert scan_events[0].aggregate_id == ARTIFACT_A
            available = await artifact_store.complete_scan(
                tenant_context,
                artifact_id=ARTIFACT_A,
                status="AVAILABLE",
                object_uri=(f"artifact://tenant/{TENANT_A}/artifact/{ARTIFACT_A}"),
                scan_result={
                    "schema_version": "artifact-scan/v1",
                    "decision": "PASSED",
                    "scanned_at": scan_now.isoformat(),
                },
                now=scan_now,
            )
            assert available.status == "AVAILABLE"
            assert (
                await artifact_store.reclaim_expired_uploads(
                    tenant_context,
                    now=artifact_now + timedelta(minutes=16),
                    limit=10,
                )
                == 0
            )
            await artifact_outbox.mark_published(
                tenant_context, scan_events[0].id, now=scan_now
            )
            download_grant_id = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
            download_token_hash = "sha256:" + "d" * 64
            download_confirmed = await artifact_store.create_download_grant(
                tenant_context,
                grant_id=download_grant_id,
                artifact_id=ARTIFACT_A,
                owner_user_id=UUID(USER_A),
                token_hash=download_token_hash,
                grant_expires_at=scan_now + timedelta(minutes=5),
                metadata=RequestMetadata(
                    request_id="req-artifact-download",
                    trace_id="trace-artifact-download",
                ),
                now=scan_now,
            )
            assert download_confirmed is True
            resolved_download = await artifact_store.resolve_download_grant(
                grant_id=download_grant_id,
                token_hash=download_token_hash,
                service_subject_id=UUID(USER_A),
                metadata=RequestMetadata(
                    request_id="req-artifact-download-resolve",
                    trace_id="trace-artifact-download-resolve",
                ),
                now=scan_now,
            )
            assert resolved_download is not None
            assert resolved_download.artifact_id == ARTIFACT_A
            opened_download = await artifact_store.record_download_open(
                tenant_context,
                grant_id=download_grant_id,
                artifact_id=ARTIFACT_A,
                metadata=RequestMetadata(
                    request_id="req-artifact-download-open",
                    trace_id="trace-artifact-download-open",
                ),
                now=scan_now,
            )
            assert opened_download is True
            assert await artifact_store.is_download_grant_active(
                tenant_context,
                grant_id=download_grant_id,
                artifact_id=ARTIFACT_A,
                now=scan_now,
            )

            expired = await artifact_store.get_downloadable(
                tenant_context,
                artifact_id=ARTIFACT_A,
                owner_user_id=UUID(USER_A),
                metadata=RequestMetadata(
                    request_id="req-artifact-expire",
                    trace_id="trace-artifact-expire",
                ),
                now=artifact_now + timedelta(days=31),
            )
            assert expired is not None
            assert expired.status == "EXPIRED"
            deletion = await artifact_store.request_delete(
                tenant_context,
                artifact_id=ARTIFACT_A,
                owner_user_id=UUID(USER_A),
                idempotency_key="postgresql-artifact-delete",
                request_hash="artifact-delete-hash",
                metadata=RequestMetadata(
                    request_id="req-artifact-delete",
                    trace_id="trace-artifact-delete",
                ),
                now=artifact_now + timedelta(days=31, seconds=1),
            )
            assert deletion is not None
            assert deletion.value is not None
            assert deletion.value.status == "ACCEPTED"
            delete_operation_id = deletion.value.id
            revoked_download = await artifact_store.resolve_download_grant(
                grant_id=download_grant_id,
                token_hash=download_token_hash,
                service_subject_id=UUID(USER_A),
                metadata=RequestMetadata(
                    request_id="req-artifact-download-revoked-resolve",
                    trace_id="trace-artifact-download-revoked-resolve",
                ),
                now=scan_now,
            )
            assert revoked_download is None
            assert not await artifact_store.is_download_grant_active(
                tenant_context,
                grant_id=download_grant_id,
                artifact_id=ARTIFACT_A,
                now=scan_now,
            )
            replayed_deletion = await artifact_store.request_delete(
                tenant_context,
                artifact_id=ARTIFACT_A,
                owner_user_id=UUID(USER_A),
                idempotency_key="postgresql-artifact-delete",
                request_hash="artifact-delete-hash",
                metadata=RequestMetadata(
                    request_id="req-artifact-delete-replay",
                    trace_id="trace-artifact-delete-replay",
                ),
                now=artifact_now + timedelta(days=31, seconds=2),
            )
            assert replayed_deletion is not None
            assert replayed_deletion.replay is not None
            delete_outbox = SqlAlchemyOutboxStore(
                policy_factory,
                event_types=frozenset({ARTIFACT_DELETE_REQUESTED_EVENT}),
            )
            delete_events = await delete_outbox.claim_ready(
                tenant_context,
                now=artifact_now + timedelta(days=31, seconds=2),
                limit=10,
                lease_duration=timedelta(seconds=30),
            )
            assert len(delete_events) == 1
            revocation_outbox = SqlAlchemyOutboxStore(
                policy_factory,
                event_types=frozenset({ARTIFACT_DOWNLOADS_REVOKED_EVENT}),
            )
            revocation_events = await revocation_outbox.claim_ready(
                tenant_context,
                now=artifact_now + timedelta(days=31, seconds=2),
                limit=10,
                lease_duration=timedelta(seconds=30),
            )
            assert len(revocation_events) == 1
            assert revocation_events[0].aggregate_id == ARTIFACT_A
            cleanup_target = await artifact_store.get_for_delete(
                tenant_context,
                artifact_id=ARTIFACT_A,
                operation_id=delete_operation_id,
            )
            assert cleanup_target is not None
            assert cleanup_target.status == "DELETING"
            await artifact_store.complete_delete(
                tenant_context,
                artifact_id=ARTIFACT_A,
                operation_id=delete_operation_id,
                now=artifact_now + timedelta(days=31, seconds=3),
            )
            await delete_outbox.mark_published(
                tenant_context,
                delete_events[0].id,
                now=artifact_now + timedelta(days=31, seconds=3),
            )

            async with app_engine.begin() as connection:
                await connection.execute(
                    text(
                        "SELECT set_config"
                        "('app.current_tenant_id', :tenant_id, true)"
                    ),
                    {"tenant_id": TENANT_B},
                )
                visible_artifacts = (
                    await connection.execute(text("SELECT id FROM artifact"))
                ).scalars()
                assert list(visible_artifacts) == []

            async with app_engine.begin() as connection:
                await connection.execute(
                    text(
                        "SELECT set_config"
                        "('app.current_tenant_id', :tenant_id, true)"
                    ),
                    {"tenant_id": TENANT_A},
                )
                lifecycle_state = (
                    await connection.execute(
                        text(
                            "SELECT a.status, a.deleted_at IS NOT NULL, o.status "
                            "FROM artifact a JOIN operation_record o "
                            "ON o.tenant_id = a.tenant_id "
                            "AND o.resource_id = a.id "
                            "WHERE a.id = :artifact_id AND o.id = :operation_id"
                        ),
                        {
                            "artifact_id": ARTIFACT_A,
                            "operation_id": delete_operation_id,
                        },
                    )
                ).one()
                assert tuple(lifecycle_state) == ("DELETED", True, "SUCCEEDED")

            failed_artifact = await artifact_store.create_upload(
                tenant_context,
                artifact_id=ARTIFACT_FAILED,
                owner_user_id=UUID(USER_A),
                request=artifact_request,
                quarantine_object_uri=(
                    f"quarantine://tenant/{TENANT_A}/artifact/"
                    f"{ARTIFACT_FAILED}/source"
                ),
                upload_expires_at=artifact_now + timedelta(minutes=15),
                expires_at=artifact_now + timedelta(days=30),
                idempotency_key="postgresql-artifact-failed-create",
                request_hash="artifact-failed-create-hash",
                metadata=RequestMetadata(
                    request_id="req-artifact-failed-create",
                    trace_id="trace-artifact-failed-create",
                ),
            )
            await artifact_store.fail_upload(
                tenant_context,
                artifact_id=failed_artifact.id,
                owner_user_id=UUID(USER_A),
                code="ARTIFACT_UPLOAD_MISMATCH",
                now=artifact_now + timedelta(seconds=1),
            )
            failed_deletion = await artifact_store.request_delete(
                tenant_context,
                artifact_id=failed_artifact.id,
                owner_user_id=UUID(USER_A),
                idempotency_key="postgresql-artifact-failed-delete",
                request_hash="artifact-failed-delete-hash",
                metadata=RequestMetadata(
                    request_id="req-artifact-failed-delete",
                    trace_id="trace-artifact-failed-delete",
                ),
                now=artifact_now + timedelta(seconds=2),
            )
            assert failed_deletion is not None
            assert failed_deletion.value is not None
            failed_cleanup = await artifact_store.get_for_delete(
                tenant_context,
                artifact_id=failed_artifact.id,
                operation_id=failed_deletion.value.id,
            )
            assert failed_cleanup is not None
            assert failed_cleanup.status == "DELETING"
            assert failed_cleanup.object_uri is None
            await artifact_store.complete_delete(
                tenant_context,
                artifact_id=failed_artifact.id,
                operation_id=failed_deletion.value.id,
                now=artifact_now + timedelta(seconds=3),
            )
            failed_delete_outbox = SqlAlchemyOutboxStore(
                policy_factory,
                event_types=frozenset({ARTIFACT_DELETE_REQUESTED_EVENT}),
            )
            failed_delete_events = await failed_delete_outbox.claim_ready(
                tenant_context,
                now=artifact_now + timedelta(seconds=3),
                limit=10,
                lease_duration=timedelta(seconds=30),
            )
            assert len(failed_delete_events) == 1
            await failed_delete_outbox.mark_published(
                tenant_context,
                failed_delete_events[0].id,
                now=artifact_now + timedelta(seconds=3),
            )

            expired_upload_request = artifact_request.model_copy(update={"size": 24})
            expired_upload = await artifact_store.create_upload(
                tenant_context,
                artifact_id=ARTIFACT_EXPIRED_UPLOAD,
                owner_user_id=UUID(USER_A),
                request=expired_upload_request,
                quarantine_object_uri=(
                    f"quarantine://tenant/{TENANT_A}/artifact/"
                    f"{ARTIFACT_EXPIRED_UPLOAD}/source"
                ),
                upload_expires_at=artifact_now + timedelta(seconds=10),
                expires_at=artifact_now + timedelta(days=30),
                idempotency_key="postgresql-artifact-expired-upload-create",
                request_hash="artifact-expired-upload-create-hash",
                metadata=RequestMetadata(
                    request_id="req-artifact-expired-upload-create",
                    trace_id="trace-artifact-expired-upload-create",
                ),
            )
            assert expired_upload.status == "UPLOADING"
            assert (
                await artifact_store.reclaim_expired_uploads(
                    tenant_context,
                    now=artifact_now + timedelta(seconds=9),
                    limit=10,
                )
                == 0
            )
            reclaimed = await asyncio.gather(
                artifact_store.reclaim_expired_uploads(
                    tenant_context,
                    now=artifact_now + timedelta(seconds=11),
                    limit=10,
                ),
                artifact_store.reclaim_expired_uploads(
                    tenant_context,
                    now=artifact_now + timedelta(seconds=11),
                    limit=10,
                ),
            )
            assert sum(reclaimed) == 1
            assert (
                await artifact_store.reclaim_expired_uploads(
                    tenant_context,
                    now=artifact_now + timedelta(seconds=12),
                    limit=10,
                )
                == 0
            )
            async with TenantUnitOfWork(
                policy_factory, tenant_context, read_only=True
            ) as unit:
                reclaimed_state = (
                    await unit.session.execute(
                        select(
                            ArtifactModel.status,
                            ArtifactModel.scan_result_json,
                            OperationRecordModel.id,
                            OperationRecordModel.status,
                        )
                        .join(
                            OperationRecordModel,
                            OperationRecordModel.resource_id == ArtifactModel.id,
                        )
                        .where(
                            ArtifactModel.tenant_id == UUID(TENANT_A),
                            ArtifactModel.id == ARTIFACT_EXPIRED_UPLOAD,
                            OperationRecordModel.tenant_id == UUID(TENANT_A),
                            OperationRecordModel.operation_type == "artifact.delete",
                        )
                    )
                ).one()
                assert reclaimed_state[0] == "DELETING"
                assert reclaimed_state[1]["failure_code"] == "ARTIFACT_UPLOAD_EXPIRED"
                assert reclaimed_state[3] == "ACCEPTED"
                assert (
                    await unit.session.scalar(
                        select(func.count(OutboxEventModel.id)).where(
                            OutboxEventModel.tenant_id == UUID(TENANT_A),
                            OutboxEventModel.aggregate_id == ARTIFACT_EXPIRED_UPLOAD,
                            OutboxEventModel.event_type
                            == ARTIFACT_DELETE_REQUESTED_EVENT,
                        )
                    )
                    == 1
                )
                assert (
                    await unit.session.scalar(
                        select(func.count(OperationRecordModel.id)).where(
                            OperationRecordModel.tenant_id == UUID(TENANT_A),
                            OperationRecordModel.resource_id == ARTIFACT_EXPIRED_UPLOAD,
                            OperationRecordModel.operation_type == "artifact.delete",
                        )
                    )
                    == 1
                )
            expired_delete_outbox = SqlAlchemyOutboxStore(
                policy_factory,
                event_types=frozenset({ARTIFACT_DELETE_REQUESTED_EVENT}),
            )
            expired_delete_events = await expired_delete_outbox.claim_ready(
                tenant_context,
                now=artifact_now + timedelta(seconds=12),
                limit=10,
                lease_duration=timedelta(seconds=30),
            )
            assert len(expired_delete_events) == 1
            assert expired_delete_events[0].aggregate_id == ARTIFACT_EXPIRED_UPLOAD
            deletion_recorder = ArtifactDeletionRecorder()
            await ArtifactDeleteProcessor(artifact_store, deletion_recorder).process(
                tenant_context,
                expired_delete_events[0],
                now=artifact_now + timedelta(seconds=13),
            )
            await expired_delete_outbox.mark_published(
                tenant_context,
                expired_delete_events[0].id,
                now=artifact_now + timedelta(seconds=13),
            )
            assert deletion_recorder.deleted == [ARTIFACT_EXPIRED_UPLOAD]
            replacement = await artifact_store.create_upload(
                tenant_context,
                artifact_id=ARTIFACT_REPLACEMENT,
                owner_user_id=UUID(USER_A),
                request=expired_upload_request,
                quarantine_object_uri=(
                    f"quarantine://tenant/{TENANT_A}/artifact/"
                    f"{ARTIFACT_REPLACEMENT}/source"
                ),
                upload_expires_at=artifact_now + timedelta(minutes=15),
                expires_at=artifact_now + timedelta(days=30),
                idempotency_key="postgresql-artifact-replacement-create",
                request_hash="artifact-replacement-create-hash",
                metadata=RequestMetadata(
                    request_id="req-artifact-replacement-create",
                    trace_id="trace-artifact-replacement-create",
                ),
            )
            assert replacement.status == "UPLOADING"

            budget_guard = SqlAlchemyModelBudgetGuard(
                policy_factory,
                clock=lambda: datetime(2026, 8, 7, 12, 0, 10, tzinfo=UTC),
            )
            rate_limiter = SqlAlchemyModelRateLimiter(
                policy_factory,
                clock=lambda: datetime(2026, 8, 7, 12, 0, 10, tzinfo=UTC),
            )
            gateway_request = ModelGatewayRequest(
                schema_version="1.0",
                tenant_id=TENANT_A,
                user_id="user-a",
                agent_id="agent-a",
                snapshot_id="snapshot-a",
                run_id="run-admission-a",
                model_binding_id="binding-a",
                prompt_ref=ImmutableReference(uri="prompt://a", hash=HASH),
                tools_ref=ImmutableReference(uri="tools://a", hash=HASH),
                capability_requirements=[],
                stream=False,
                timeout_seconds=30,
                token_budget=10,
                idempotency_key="admission-idem-a",
                authorization_token="authorization-token-1234",
            )
            binding = ModelBinding(
                binding_id="binding-a",
                routes=(
                    ModelRoute(
                        provider="openai",
                        model="model-a",
                        base_url="https://provider.test/v1",
                        secret_ref="secret://tenant/a/model",
                        capabilities=frozenset(),
                        timeout_seconds=30,
                        rate_limit_rpm=1,
                    ),
                ),
            )
            permit = await budget_guard.authorize(
                tenant_context, gateway_request, binding
            )
            assert permit.reserved_tokens == 10
            with pytest.raises(ProviderError) as exhausted:
                await budget_guard.authorize(
                    tenant_context,
                    gateway_request.model_copy(
                        update={"idempotency_key": "admission-idem-b"}
                    ),
                    binding,
                )
            assert exhausted.value.code == "TOKEN_BUDGET_EXCEEDED"
            await SqlAlchemyModelGatewayStore(policy_factory).record(
                tenant_context,
                ModelUsageRecord(
                    id=UUID("88888888-8888-4888-8888-888888888888"),
                    tenant_id=UUID(TENANT_A),
                    run_id=gateway_request.run_id,
                    provider="openai",
                    model="model-a",
                    provider_request_id="provider-request-admission",
                    input_tokens=3,
                    output_tokens=2,
                    reasoning_tokens=0,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    token_estimated=False,
                    cost_amount=None,
                    cost_currency=None,
                    started_at=datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
                    finished_at=datetime(2026, 8, 7, 12, 0, 5, tzinfo=UTC),
                ),
            )
            assert (
                await budget_guard.settle(
                    tenant_context,
                    gateway_request,
                    permit,
                    ModelUsage(
                        input_tokens=3,
                        output_tokens=2,
                        reasoning_tokens=0,
                        cache_read_tokens=0,
                        cache_write_tokens=0,
                        estimated=False,
                    ),
                )
                is None
            )
            released_request = gateway_request.model_copy(
                update={"idempotency_key": "admission-idem-b"}
            )
            released = await budget_guard.authorize(
                tenant_context, released_request, binding
            )
            assert released.reserved_tokens == 5
            await budget_guard.release(tenant_context, released)

            await rate_limiter.acquire(
                tenant_context, gateway_request, binding, binding.routes[0]
            )
            with pytest.raises(ProviderError) as limited:
                await rate_limiter.acquire(
                    tenant_context, gateway_request, binding, binding.routes[0]
                )
            assert limited.value.code == "GATEWAY_RATE_LIMITED"

            concurrent_request = gateway_request.model_copy(
                update={
                    "run_id": "run-admission-concurrent",
                    "idempotency_key": "admission-concurrent-a",
                }
            )
            concurrent_results = await asyncio.gather(
                budget_guard.authorize(tenant_context, concurrent_request, binding),
                budget_guard.authorize(
                    tenant_context,
                    concurrent_request.model_copy(
                        update={"idempotency_key": "admission-concurrent-b"}
                    ),
                    binding,
                ),
                return_exceptions=True,
            )
            concurrent_permits = [
                result
                for result in concurrent_results
                if isinstance(result, BudgetPermit)
            ]
            concurrent_errors = [
                result
                for result in concurrent_results
                if isinstance(result, ProviderError)
            ]
            assert len(concurrent_permits) == 1
            assert [error.code for error in concurrent_errors] == [
                "TOKEN_BUDGET_EXCEEDED"
            ]
            await budget_guard.release(tenant_context, concurrent_permits[0])

            async with app_engine.begin() as connection:
                await connection.execute(
                    text(
                        "SELECT set_config('app.current_tenant_id', :tenant_id, true)"
                    ),
                    {"tenant_id": TENANT_A},
                )
                await connection.execute(text("SET CONSTRAINTS ALL DEFERRED"))
                await connection.execute(
                    text(
                        "INSERT INTO budget_policy "
                        "(id, tenant_id, name, status, current_version_id, created_by) "
                        "VALUES (:policy_id, :tenant_id, 'Daily model tokens', "
                        "'ACTIVE', :version_id, :created_by)"
                    ),
                    {
                        "policy_id": BUDGET_POLICY_A,
                        "tenant_id": TENANT_A,
                        "version_id": BUDGET_POLICY_VERSION_A,
                        "created_by": USER_A,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO budget_policy_version "
                        "(id, tenant_id, policy_id, version_no, period, enforcement, "
                        "token_limit, content_hash, created_by) VALUES "
                        "(:version_id, :tenant_id, :policy_id, 1, 'DAILY', 'HARD', "
                        "12, :content_hash, :created_by)"
                    ),
                    {
                        "version_id": BUDGET_POLICY_VERSION_A,
                        "tenant_id": TENANT_A,
                        "policy_id": BUDGET_POLICY_A,
                        "content_hash": "sha256:" + "b" * 64,
                        "created_by": USER_A,
                    },
                )
                await connection.execute(
                    text(
                        "UPDATE tenant SET budget_policy_id = :policy_id "
                        "WHERE id = :tenant_id"
                    ),
                    {"policy_id": BUDGET_POLICY_A, "tenant_id": TENANT_A},
                )

            tenant_only_request = gateway_request.model_copy(
                update={
                    "run_id": "run-tenant-budget",
                    "token_budget": None,
                    "idempotency_key": "tenant-budget-only",
                }
            )
            tenant_only_permit = await budget_guard.authorize(
                tenant_context, tenant_only_request, binding
            )
            assert tenant_only_permit.reserved_tokens == 4
            await budget_guard.release(tenant_context, tenant_only_permit)

            tenant_concurrent_request = tenant_only_request.model_copy(
                update={"idempotency_key": "tenant-budget-concurrent-a"}
            )
            tenant_concurrent_results = await asyncio.gather(
                budget_guard.authorize(
                    tenant_context, tenant_concurrent_request, binding
                ),
                budget_guard.authorize(
                    tenant_context,
                    tenant_concurrent_request.model_copy(
                        update={
                            "run_id": "run-tenant-budget-concurrent-b",
                            "idempotency_key": "tenant-budget-concurrent-b",
                        }
                    ),
                    binding,
                ),
                return_exceptions=True,
            )
            tenant_concurrent_permits = [
                result
                for result in tenant_concurrent_results
                if isinstance(result, BudgetPermit)
            ]
            tenant_concurrent_errors = [
                result
                for result in tenant_concurrent_results
                if isinstance(result, ProviderError)
            ]
            assert len(tenant_concurrent_permits) == 1
            assert [error.code for error in tenant_concurrent_errors] == [
                "TOKEN_BUDGET_EXCEEDED"
            ]
            await budget_guard.release(tenant_context, tenant_concurrent_permits[0])

            intersected_request = tenant_only_request.model_copy(
                update={
                    "run_id": "run-intersected-budget",
                    "token_budget": 2,
                    "idempotency_key": "tenant-run-intersection",
                }
            )
            intersected_permit = await budget_guard.authorize(
                tenant_context, intersected_request, binding
            )
            assert intersected_permit.reserved_tokens == 2
            await budget_guard.release(tenant_context, intersected_permit)

            async with TenantUnitOfWork(policy_factory, tenant_context) as unit:
                reservation_snapshot = (
                    await unit.session.execute(
                        text(
                            "SELECT budget_policy_id, budget_policy_version_id, "
                            "budget_period_started_at, budget_period_ends_at "
                            "FROM budget_reservation WHERE run_id = :run_id"
                        ),
                        {"run_id": intersected_request.run_id},
                    )
                ).one()
                assert str(reservation_snapshot.budget_policy_id) == BUDGET_POLICY_A
                assert (
                    str(reservation_snapshot.budget_policy_version_id)
                    == BUDGET_POLICY_VERSION_A
                )
                assert reservation_snapshot.budget_period_started_at == datetime(
                    2026, 8, 7, tzinfo=UTC
                )
                assert reservation_snapshot.budget_period_ends_at == datetime(
                    2026, 8, 8, tzinfo=UTC
                )

            async with TenantUnitOfWork(policy_factory, tenant_context) as unit:
                await unit.session.execute(
                    text(
                        "UPDATE budget_policy SET status = 'DISABLED' "
                        "WHERE id = :policy_id"
                    ),
                    {"policy_id": BUDGET_POLICY_A},
                )
            disabled_permit = await budget_guard.authorize(
                tenant_context,
                tenant_only_request.model_copy(
                    update={"idempotency_key": "disabled-tenant-budget"}
                ),
                binding,
            )
            assert disabled_permit.reservation_id is None

            tenant_b_request = gateway_request.model_copy(
                update={
                    "tenant_id": TENANT_B,
                    "run_id": "run-admission-b",
                    "idempotency_key": "admission-idem-b-tenant",
                }
            )
            tenant_b_context = tenant_context.model_copy(update={"tenant_id": TENANT_B})
            tenant_b_permit = await budget_guard.authorize(
                tenant_b_context, tenant_b_request, binding
            )
            assert tenant_b_permit.reserved_tokens == 10
            await budget_guard.release(tenant_b_context, tenant_b_permit)
        finally:
            await app_engine.dispose()

        async with admin_engine.connect() as connection:
            outbox_state = (
                await connection.execute(
                    text(
                        "SELECT status, attempts, published_at IS NOT NULL "
                        "FROM outbox_event WHERE id = :id"
                    ),
                    {"id": OUTBOX_A},
                )
            ).one()
            assert tuple(outbox_state) == ("PUBLISHED", 1, True)
            policies = (
                await connection.execute(
                    text(
                        "SELECT tablename FROM pg_policies "
                        "WHERE policyname = 'tenant_isolation' ORDER BY tablename"
                    )
                )
            ).scalars()
            assert list(policies) == [
                "agent_binding",
                "agent_definition",
                "agent_run",
                "agent_snapshot",
                "agent_version",
                "approval_decision",
                "approval_request",
                "artifact",
                "artifact_download_grant",
                "audit_log",
                "budget_policy",
                "budget_policy_version",
                "budget_reservation",
                "chat_message",
                "chat_session",
                "deployment",
                "execution_ticket",
                "mcp_capability_discovery",
                "model_binding_snapshot",
                "model_rate_limit_window",
                "model_usage",
                "operation_record",
                "outbox_event",
                "price_catalog_rate",
                "price_catalog_version",
                "quota_policy",
                "quota_policy_version",
                "release",
                "resource_definition",
                "resource_version",
                "role",
                "role_binding",
                "role_permission",
                "run_admission_queue",
                "run_attempt",
                "run_event",
                "run_event_counter",
                "runtime_bundle",
                "runtime_checkpoint",
                "sandbox_instance",
                "sandbox_lease",
                "skill_supply_chain_scan",
                "storage_policy",
                "storage_policy_version",
                "tenant_member",
                "workspace",
            ]
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

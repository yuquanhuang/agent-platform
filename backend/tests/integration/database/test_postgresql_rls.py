"""Real PostgreSQL migration and RLS isolation verification."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from packages.application.model_gateway import BudgetPermit
from packages.contracts.model_gateway import (
    ImmutableReference,
    ModelGatewayRequest,
    ModelUsage,
)
from packages.contracts.public import SubjectType, TenantContext
from packages.domain.model_gateway import (
    ModelBinding,
    ModelRoute,
    ModelUsageRecord,
    ProviderError,
)
from packages.infrastructure.database.outbox import SqlAlchemyOutboxStore
from packages.infrastructure.database.public import create_session_factory
from packages.infrastructure.model_gateway import (
    SqlAlchemyModelBudgetGuard,
    SqlAlchemyModelGatewayStore,
    SqlAlchemyModelRateLimiter,
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
HASH = "sha256:" + "a" * 64


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
                "agent_snapshot",
                "agent_version",
                "budget_reservation",
                "deployment",
                "model_binding_snapshot",
                "model_rate_limit_window",
                "model_usage",
                "operation_record",
                "outbox_event",
                "release",
                "resource_definition",
                "resource_version",
                "role",
                "role_binding",
                "role_permission",
                "runtime_bundle",
                "tenant_member",
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

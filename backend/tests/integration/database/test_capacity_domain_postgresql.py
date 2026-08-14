"""Real PostgreSQL verification for global Run Capacity Domain leases."""

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
from sqlalchemy.ext.asyncio import create_async_engine

from packages.application.policy import RunCapacityPolicy
from packages.contracts.public import SubjectType, TenantContext
from packages.infrastructure.database.runs import SqlAlchemyRunStore
from packages.infrastructure.database.session import create_session_factory

BACKEND_ROOT = Path(__file__).resolve().parents[3]
DATABASE_URL_ENV = "AP_TEST_DATABASE_URL"
APP_ROLE = "agent_platform_capacity_test"
APP_PASSWORD = "capacity-test-only"
TENANT_A = UUID("11111111-1111-4111-8111-111111111111")
TENANT_B = UUID("22222222-2222-4222-8222-222222222222")
ACTOR = UUID("33333333-3333-4333-8333-333333333333")
DOMAIN = "rt_agentscope_shared"
NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)


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


def downgrade(database_url: str, revision: str) -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.downgrade(config, revision)


async def drop_test_role(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            role_exists = await connection.scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role_name"),
                {"role_name": APP_ROLE},
            )
            if role_exists is not None:
                await connection.execute(text(f"DROP OWNED BY {APP_ROLE}"))
                await connection.execute(text(f"DROP ROLE {APP_ROLE}"))
    finally:
        await engine.dispose()


def scheduler_context() -> TenantContext:
    return TenantContext(
        tenant_id=str(TENANT_A),
        subject_type=SubjectType.SERVICE,
        subject_id=str(ACTOR),
        auth_time=NOW,
        request_id="req-capacity-domain-postgresql",
        trace_id="trace-capacity-domain-postgresql",
    )


async def seed_capacity_fixture(
    database_url: str, *, use_legacy_queue_domains: bool = False
) -> dict[str, UUID]:
    ids = {
        "agent_a": UUID("aaaaaaaa-0000-4000-8000-000000000001"),
        "agent_b": UUID("bbbbbbbb-0000-4000-8000-000000000001"),
        "snapshot_a": UUID("aaaaaaaa-0000-4000-8000-000000000002"),
        "snapshot_b": UUID("bbbbbbbb-0000-4000-8000-000000000002"),
        "deployment_a": UUID("aaaaaaaa-0000-4000-8000-000000000003"),
        "deployment_b": UUID("bbbbbbbb-0000-4000-8000-000000000003"),
        "session_a": UUID("aaaaaaaa-0000-4000-8000-000000000004"),
        "session_b": UUID("bbbbbbbb-0000-4000-8000-000000000004"),
        "message_a": UUID("aaaaaaaa-0000-4000-8000-000000000005"),
        "message_b": UUID("bbbbbbbb-0000-4000-8000-000000000005"),
        "run_a": UUID("aaaaaaaa-0000-4000-8000-000000000006"),
        "run_b": UUID("bbbbbbbb-0000-4000-8000-000000000006"),
        "queue_a": UUID("aaaaaaaa-0000-4000-8000-000000000007"),
        "queue_b": UUID("bbbbbbbb-0000-4000-8000-000000000007"),
    }
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
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
                    f"GRANT USAGE, SELECT ON ALL SEQUENCES "
                    f"IN SCHEMA public TO {APP_ROLE}"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO tenant (id, code, name) VALUES "
                    "(:tenant_a, 'capacity-a', 'Capacity A'), "
                    "(:tenant_b, 'capacity-b', 'Capacity B')"
                ),
                {"tenant_a": TENANT_A, "tenant_b": TENANT_B},
            )
            await connection.execute(
                text(
                    "INSERT INTO app_user "
                    "(id, identity_issuer, external_subject, display_name) VALUES "
                    "(:actor, 'https://issuer.test', 'capacity-owner', 'Capacity Owner')"
                ),
                {"actor": ACTOR},
            )
            await connection.execute(
                text(
                    "INSERT INTO tenant_member (tenant_id, user_id) VALUES "
                    "(:tenant_a, :actor), (:tenant_b, :actor)"
                ),
                {"tenant_a": TENANT_A, "tenant_b": TENANT_B, "actor": ACTOR},
            )
            for suffix, tenant_id in (("a", TENANT_A), ("b", TENANT_B)):
                await connection.execute(
                    text(
                        "INSERT INTO agent_definition "
                        "(id, tenant_id, code, name, runtime_type, visibility, "
                        "owner_user_id, status, created_by, updated_by) VALUES "
                        "(:agent, :tenant, :code, :name, 'agentscope', 'private', "
                        ":actor, 'ACTIVE', :actor, :actor)"
                    ),
                    {
                        "agent": ids[f"agent_{suffix}"],
                        "tenant": tenant_id,
                        "code": f"capacity_agent_{suffix}",
                        "name": f"Capacity Agent {suffix.upper()}",
                        "actor": ACTOR,
                    },
                )
                version_id = UUID(
                    f"{str(ids[f'agent_{suffix}'])[:8]}-0000-4000-8000-000000000011"
                )
                await connection.execute(
                    text(
                        "INSERT INTO agent_version "
                        "(id, tenant_id, agent_id, version_no, created_by) VALUES "
                        "(:version, :tenant, :agent, 1, :actor)"
                    ),
                    {
                        "version": version_id,
                        "tenant": tenant_id,
                        "agent": ids[f"agent_{suffix}"],
                        "actor": ACTOR,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO agent_snapshot "
                        "(id, tenant_id, agent_version_id, schema_version, content_json, "
                        "content_hash, compiler_input_hash, created_by) VALUES "
                        "(:snapshot, :tenant, :version, '1.0', '{}'::jsonb, "
                        ":hash, :hash, :actor)"
                    ),
                    {
                        "snapshot": ids[f"snapshot_{suffix}"],
                        "tenant": tenant_id,
                        "version": version_id,
                        "hash": "sha256:" + suffix * 64,
                        "actor": ACTOR,
                    },
                )
                operation_id = UUID(
                    f"{str(ids[f'agent_{suffix}'])[:8]}-0000-4000-8000-000000000012"
                )
                release_id = UUID(
                    f"{str(ids[f'agent_{suffix}'])[:8]}-0000-4000-8000-000000000013"
                )
                bundle_id = UUID(
                    f"{str(ids[f'agent_{suffix}'])[:8]}-0000-4000-8000-000000000014"
                )
                await connection.execute(
                    text(
                        "INSERT INTO operation_record "
                        "(id, tenant_id, actor_id, operation_type, status) VALUES "
                        "(:operation, :tenant, :actor, 'agent.publish', 'SUCCEEDED')"
                    ),
                    {"operation": operation_id, "tenant": tenant_id, "actor": ACTOR},
                )
                await connection.execute(
                    text(
                        "INSERT INTO release "
                        "(id, tenant_id, agent_id, requested_by, operation_id, release_kind, "
                        "expected_agent_version, runtime_targets_json, release_note, "
                        "run_smoke_test, activate_on_success, status, workflow_id, snapshot_id) "
                        "VALUES (:release, :tenant, :agent, :actor, :operation, 'PUBLISH', 1, "
                        ":targets, 'capacity fixture', false, true, 'SUCCEEDED', :workflow, "
                        ":snapshot)"
                    ),
                    {
                        "release": release_id,
                        "tenant": tenant_id,
                        "agent": ids[f"agent_{suffix}"],
                        "actor": ACTOR,
                        "operation": operation_id,
                        "targets": [DOMAIN],
                        "workflow": f"release/capacity/{suffix}",
                        "snapshot": ids[f"snapshot_{suffix}"],
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO runtime_bundle "
                        "(id, tenant_id, snapshot_id, runtime_type, compiler_name, "
                        "compiler_version, manifest_schema_version, manifest_json, "
                        "content_hash, object_uri, size_bytes, scan_status) VALUES "
                        "(:bundle, :tenant, :snapshot, 'agentscope', 'test', '1', '1.0', "
                        "'{}'::jsonb, :hash, :uri, 1, 'PASSED')"
                    ),
                    {
                        "bundle": bundle_id,
                        "tenant": tenant_id,
                        "snapshot": ids[f"snapshot_{suffix}"],
                        "hash": "sha256:" + ("c" if suffix == "a" else "d") * 64,
                        "uri": f"memory://capacity/{suffix}",
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO deployment "
                        "(id, tenant_id, release_id, agent_id, snapshot_id, bundle_id, "
                        "runtime_target_id, status, compatibility_hash, "
                        "activation_fencing_token, activated_at) VALUES "
                        "(:deployment, :tenant, :release, :agent, :snapshot, :bundle, "
                        ":domain, 'ACTIVE', :hash, :token, :now)"
                    ),
                    {
                        "deployment": ids[f"deployment_{suffix}"],
                        "tenant": tenant_id,
                        "release": release_id,
                        "agent": ids[f"agent_{suffix}"],
                        "snapshot": ids[f"snapshot_{suffix}"],
                        "bundle": bundle_id,
                        "domain": DOMAIN,
                        "hash": "sha256:" + "e" * 64,
                        "token": 1 if suffix == "a" else 2,
                        "now": NOW,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO chat_session "
                        "(id, tenant_id, user_id, agent_id, default_deployment_id, title) "
                        "VALUES (:session, :tenant, :actor, :agent, :deployment, :title)"
                    ),
                    {
                        "session": ids[f"session_{suffix}"],
                        "tenant": tenant_id,
                        "actor": ACTOR,
                        "agent": ids[f"agent_{suffix}"],
                        "deployment": ids[f"deployment_{suffix}"],
                        "title": f"Capacity {suffix.upper()}",
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO chat_message "
                        "(id, tenant_id, session_id, role, content_parts_json, created_by) "
                        "VALUES (:message, :tenant, :session, 'USER', :content, :actor)"
                    ),
                    {
                        "message": ids[f"message_{suffix}"],
                        "tenant": tenant_id,
                        "session": ids[f"session_{suffix}"],
                        "content": [{"type": "text", "text": f"capacity {suffix}"}],
                        "actor": ACTOR,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO agent_run "
                        "(id, tenant_id, session_id, user_message_id, agent_id, snapshot_id, "
                        "deployment_id, status, idempotency_key, created_by, created_at, "
                        "queued_at) VALUES (:run, :tenant, :session, :message, :agent, "
                        ":snapshot, :deployment, 'QUEUED', :idempotency, :actor, :now, :now)"
                    ),
                    {
                        "run": ids[f"run_{suffix}"],
                        "tenant": tenant_id,
                        "session": ids[f"session_{suffix}"],
                        "message": ids[f"message_{suffix}"],
                        "agent": ids[f"agent_{suffix}"],
                        "snapshot": ids[f"snapshot_{suffix}"],
                        "deployment": ids[f"deployment_{suffix}"],
                        "idempotency": f"capacity-run-{suffix}",
                        "actor": ACTOR,
                        "now": NOW,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO run_admission_queue "
                        "(id, tenant_id, run_id, capacity_domain, status, queued_at, "
                        "deadline_at, created_at, updated_at) VALUES "
                        "(:queue, :tenant, :run, :domain, 'WAITING', :now, :deadline, "
                        ":now, :now)"
                    ),
                    {
                        "queue": ids[f"queue_{suffix}"],
                        "tenant": tenant_id,
                        "run": ids[f"run_{suffix}"],
                        "domain": (
                            f"tenant/{tenant_id}"
                            if use_legacy_queue_domains
                            else DOMAIN
                        ),
                        "now": NOW,
                        "deadline": NOW + timedelta(minutes=5),
                    },
                )
    finally:
        await engine.dispose()
    return ids


async def verify_capacity_domain(database_url: str, ids: dict[str, UUID]) -> None:
    app_url = make_url(database_url).set(username=APP_ROLE, password=APP_PASSWORD)
    app_engine = create_async_engine(app_url)
    admin_engine = create_async_engine(database_url)
    try:
        factory = create_session_factory(app_engine)
        store_a = SqlAlchemyRunStore(
            factory,
            run_capacity_policy=RunCapacityPolicy(
                max_nonterminal_runs_per_tenant=1_000_000
            ),
            capacity_domain_slots={DOMAIN: 1},
            capacity_lease_ttl=timedelta(seconds=60),
        )
        store_b = SqlAlchemyRunStore(
            factory,
            run_capacity_policy=RunCapacityPolicy(
                max_nonterminal_runs_per_tenant=1_000_000
            ),
            capacity_domain_slots={DOMAIN: 1},
            capacity_lease_ttl=timedelta(seconds=60),
        )

        results = await asyncio.gather(
            store_a.process_capacity_admission_domains(
                scheduler_context(), now=NOW, limit=20
            ),
            store_b.process_capacity_admission_domains(
                scheduler_context(), now=NOW, limit=20
            ),
        )
        assert sorted(results) == [(0, 0, 0, 0), (1, 0, 0, 0)]

        async with admin_engine.connect() as connection:
            admitted = (
                await connection.execute(
                    text(
                        "SELECT tenant_id, run_id, capacity_snapshot_json "
                        "FROM run_admission_queue WHERE status = 'ADMITTED'"
                    )
                )
            ).one()
            waiting_run_id = await connection.scalar(
                text("SELECT run_id FROM run_admission_queue WHERE status = 'WAITING'")
            )
            facts = (
                await connection.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM run_capacity_lease "
                        " WHERE released_at IS NULL) AS lease_count, "
                        "(SELECT count(*) FROM outbox_event WHERE event_type = "
                        " 'agent.run_requested.v1') AS outbox_count, "
                        "(SELECT count(*) FROM audit_log WHERE action = "
                        " 'run.queue.admitted') AS audit_count"
                    )
                )
            ).one()
        assert waiting_run_id is not None
        assert facts == (1, 1, 1)
        assert admitted.capacity_snapshot_json["capacity_domain"] == DOMAIN
        assert admitted.capacity_snapshot_json["capacity_domain_slots"] == 1
        assert admitted.capacity_snapshot_json["capacity_lease_id"]

        renewed = await store_a.process_capacity_admission_domains(
            scheduler_context(), now=NOW + timedelta(seconds=61), limit=20
        )
        assert renewed == (0, 0, 0, 1)
        async with admin_engine.connect() as connection:
            renewed_facts = (
                await connection.execute(
                    text(
                        "SELECT expires_at, released_at, resource_version "
                        "FROM run_capacity_lease WHERE run_id = :run_id"
                    ),
                    {"run_id": admitted.run_id},
                )
            ).one()
        assert renewed_facts.expires_at == NOW + timedelta(seconds=121)
        assert renewed_facts.released_at is None
        assert renewed_facts.resource_version == 2

        async with admin_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE agent_run SET status = 'TIMEOUT', finished_at = :now "
                    "WHERE id = :run_id"
                ),
                {"now": NOW + timedelta(seconds=62), "run_id": admitted.run_id},
            )
        released = await store_a.process_capacity_admission_domains(
            scheduler_context(), now=NOW + timedelta(seconds=63), limit=20
        )
        assert released == (1, 0, 1, 0)
        async with admin_engine.connect() as connection:
            release_facts = (
                await connection.execute(
                    text(
                        "SELECT released_at, release_reason FROM run_capacity_lease "
                        "WHERE run_id = :run_id"
                    ),
                    {"run_id": admitted.run_id},
                )
            ).one()
            next_queue = (
                await connection.execute(
                    text(
                        "SELECT status, capacity_snapshot_json FROM run_admission_queue "
                        "WHERE run_id = :run_id"
                    ),
                    {"run_id": waiting_run_id},
                )
            ).one()
        assert release_facts.release_reason == "RUN_TERMINAL"
        assert release_facts.released_at == NOW + timedelta(seconds=63)
        assert next_queue.status == "ADMITTED"
        assert next_queue.capacity_snapshot_json["capacity_lease_id"]

        async with app_engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant, true)"),
                {"tenant": admitted.tenant_id},
            )
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM run_capacity_domain")
                )
                == 0
            )
            assert (
                await connection.scalar(text("SELECT count(*) FROM run_capacity_lease"))
                == 1
            )
        async with app_engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant, true)"),
                {"tenant": TENANT_B if admitted.tenant_id == TENANT_A else TENANT_A},
            )
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM run_capacity_domain")
                )
                == 0
            )
            assert (
                await connection.scalar(text("SELECT count(*) FROM run_capacity_lease"))
                == 1
            )
        async with app_engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.platform_context', 'true', true)")
            )
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM run_capacity_domain")
                )
                == 1
            )
            assert (
                await connection.scalar(text("SELECT count(*) FROM run_capacity_lease"))
                == 2
            )

        timeout_run_id = UUID("aaaaaaaa-0000-4000-8000-000000000008")
        timeout_queue_id = UUID("aaaaaaaa-0000-4000-8000-000000000009")
        timeout_message_id = UUID("aaaaaaaa-0000-4000-8000-000000000010")
        async with admin_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO chat_message "
                    "(id, tenant_id, session_id, role, content_parts_json, created_by) "
                    "VALUES (:message, :tenant, :session, 'USER', :content, :actor)"
                ),
                {
                    "message": timeout_message_id,
                    "tenant": TENANT_A,
                    "session": ids["session_a"],
                    "content": [{"type": "text", "text": "timeout"}],
                    "actor": ACTOR,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO agent_run "
                    "(id, tenant_id, session_id, user_message_id, agent_id, snapshot_id, "
                    "deployment_id, status, idempotency_key, created_by, created_at, queued_at) "
                    "VALUES (:run, :tenant, :session, :message, :agent, :snapshot, "
                    ":deployment, 'QUEUED', 'capacity-timeout', :actor, :created, :created)"
                ),
                {
                    "run": timeout_run_id,
                    "tenant": TENANT_A,
                    "session": ids["session_a"],
                    "message": timeout_message_id,
                    "agent": ids["agent_a"],
                    "snapshot": ids["snapshot_a"],
                    "deployment": ids["deployment_a"],
                    "actor": ACTOR,
                    "created": NOW - timedelta(minutes=10),
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO run_admission_queue "
                    "(id, tenant_id, run_id, capacity_domain, status, queued_at, "
                    "deadline_at, created_at, updated_at) VALUES "
                    "(:queue, :tenant, :run, :domain, 'WAITING', :queued, :deadline, "
                    ":queued, :queued)"
                ),
                {
                    "queue": timeout_queue_id,
                    "tenant": TENANT_A,
                    "run": timeout_run_id,
                    "domain": DOMAIN,
                    "queued": NOW - timedelta(minutes=10),
                    "deadline": NOW - timedelta(minutes=5),
                },
            )
        timed_out = await store_a.process_capacity_admission_domains(
            scheduler_context(), now=NOW + timedelta(seconds=64), limit=20
        )
        assert timed_out[1] == 1
        async with admin_engine.connect() as connection:
            timeout_facts = (
                await connection.execute(
                    text(
                        "SELECT r.status, q.status AS queue_status, "
                        "(SELECT count(*) FROM run_capacity_lease l "
                        " WHERE l.run_id = r.id) AS lease_count "
                        "FROM agent_run r JOIN run_admission_queue q "
                        "ON q.tenant_id = r.tenant_id AND q.run_id = r.id "
                        "WHERE r.id = :run_id"
                    ),
                    {"run_id": timeout_run_id},
                )
            ).one()
        assert timeout_facts == ("TIMEOUT", "TIMED_OUT", 0)

        with pytest.raises(DBAPIError, match="cannot be reopened or released twice"):
            async with admin_engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE run_capacity_lease SET released_at = NULL, "
                        "release_reason = NULL, resource_version = resource_version + 1 "
                        "WHERE run_id = :run_id"
                    ),
                    {"run_id": admitted.run_id},
                )
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()


def test_capacity_domain_postgresql_integration() -> None:
    database_url = require_database_url()
    asyncio.run(drop_test_role(database_url))
    migrate(database_url, "base")
    migrate(database_url, "head")
    try:
        ids = asyncio.run(seed_capacity_fixture(database_url))
        asyncio.run(verify_capacity_domain(database_url, ids))
    finally:
        asyncio.run(drop_test_role(database_url))
        migrate(database_url, "base")


def test_capacity_domain_migration_rewrites_existing_guarded_queue() -> None:
    database_url = require_database_url()
    asyncio.run(drop_test_role(database_url))
    migrate(database_url, "base")
    migrate(database_url, "0040_artifact_retention")
    try:
        asyncio.run(seed_capacity_fixture(database_url, use_legacy_queue_domains=True))
        migrate(database_url, "0041_capacity_domain_lease")

        async def verify_upgrade_and_runtime_guard() -> None:
            engine = create_async_engine(database_url)
            try:
                async with engine.connect() as connection:
                    domains = (
                        (
                            await connection.execute(
                                text(
                                    "SELECT capacity_domain FROM run_admission_queue "
                                    "ORDER BY tenant_id"
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    assert domains == [DOMAIN, DOMAIN]
                with pytest.raises(DBAPIError, match="binding facts are immutable"):
                    async with engine.begin() as connection:
                        await connection.execute(
                            text(
                                "UPDATE run_admission_queue SET capacity_domain = "
                                "'rt_tampered' WHERE tenant_id = :tenant_id"
                            ),
                            {"tenant_id": TENANT_A},
                        )
            finally:
                await engine.dispose()

        asyncio.run(verify_upgrade_and_runtime_guard())
        downgrade(database_url, "0040_artifact_retention")

        async def verify_downgrade_and_runtime_guard() -> None:
            engine = create_async_engine(database_url)
            try:
                async with engine.connect() as connection:
                    domains = (
                        await connection.execute(
                            text(
                                "SELECT tenant_id, capacity_domain "
                                "FROM run_admission_queue ORDER BY tenant_id"
                            )
                        )
                    ).all()
                    assert domains == [
                        (TENANT_A, f"tenant/{TENANT_A}"),
                        (TENANT_B, f"tenant/{TENANT_B}"),
                    ]
                with pytest.raises(DBAPIError, match="binding facts are immutable"):
                    async with engine.begin() as connection:
                        await connection.execute(
                            text(
                                "UPDATE run_admission_queue SET capacity_domain = "
                                "'tenant/tampered' WHERE tenant_id = :tenant_id"
                            ),
                            {"tenant_id": TENANT_A},
                        )
            finally:
                await engine.dispose()

        asyncio.run(verify_downgrade_and_runtime_guard())
    finally:
        asyncio.run(drop_test_role(database_url))
        migrate(database_url, "base")

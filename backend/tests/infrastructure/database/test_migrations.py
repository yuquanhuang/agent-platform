"""Offline Alembic SQL verification for the IAM foundation migration."""

from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config

BACKEND_ROOT = Path(__file__).resolve().parents[3]


def render_upgrade_sql() -> str:
    output = StringIO()
    config = Config(str(BACKEND_ROOT / "alembic.ini"), output_buffer=output)
    config.attributes["database_url"] = (
        "postgresql+asyncpg://migration:placeholder@localhost/agent_platform"
    )

    command.upgrade(config, "head", sql=True)

    return output.getvalue()


def test_offline_upgrade_contains_foundation_tables_and_extensions() -> None:
    sql = render_upgrade_sql()

    assert "CREATE EXTENSION IF NOT EXISTS pgcrypto" in sql
    assert "CREATE EXTENSION IF NOT EXISTS citext" in sql
    for table_name in (
        "tenant",
        "app_user",
        "tenant_member",
        "role",
        "role_binding",
        "role_permission",
        "audit_log",
        "idempotency_record",
        "operation_record",
        "outbox_event",
        "resource_definition",
        "resource_version",
    ):
        assert f"CREATE TABLE {table_name}" in sql


def test_offline_upgrade_contains_membership_and_cross_tenant_constraints() -> None:
    sql = render_upgrade_sql()

    assert "membership_version BIGINT DEFAULT 1 NOT NULL" in sql
    assert "ck_tenant_member__membership_version" in sql
    assert "uq_tenant_member__tenant_id_user_id" in sql
    assert "fk_role_binding__tenant_id_role_id__role" in sql


def test_offline_upgrade_enables_and_forces_rls_with_write_checks() -> None:
    sql = render_upgrade_sql()

    for table_name in (
        "tenant_member",
        "role",
        "role_binding",
        "role_permission",
        "operation_record",
        "outbox_event",
        "resource_definition",
        "resource_version",
    ):
        assert f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY" in sql
        assert f"CREATE POLICY tenant_isolation ON {table_name}" in sql

    assert sql.count("WITH CHECK") == 8
    assert sql.count("current_setting('app.current_tenant_id', true)") == 16


def test_offline_upgrade_contains_idempotency_and_operation_constraints() -> None:
    sql = render_upgrade_sql()

    assert "uq_idempotency_record__scope" in sql
    assert "NULLS NOT DISTINCT" in sql
    assert "ck_idempotency_record__status" in sql
    assert "ck_operation_record__status" in sql
    assert "display_name VARCHAR(100)" in sql
    assert "email CITEXT" in sql


def test_offline_upgrade_contains_outbox_state_and_claim_indexes() -> None:
    sql = render_upgrade_sql()

    assert "ck_outbox_event__status" in sql
    assert "ck_outbox_event__attempts" in sql
    assert "ck_outbox_event__payload_schema_version" in sql
    assert "ix_outbox_event__status_next_attempt_at" in sql
    assert "ix_outbox_event__tenant_id_id" in sql


def test_offline_upgrade_contains_versioned_resource_registry() -> None:
    sql = render_upgrade_sql()

    assert "uq_resource_definition__tenant_type_code_active" in sql
    assert "deleted_at IS NULL" in sql
    assert "uq_resource_definition__tenant_id_id" in sql
    assert "uq_resource_version__definition_id_version_no" in sql
    assert "uq_resource_version__definition_id_content_hash" in sql
    assert "fk_resource_version__tenant_definition__resource_definition" in sql
    assert "ck_resource_version__content_hash" in sql

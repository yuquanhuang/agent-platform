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
        "agent_definition",
        "agent_binding",
        "agent_version",
        "agent_snapshot",
        "budget_reservation",
        "idempotency_record",
        "operation_record",
        "outbox_event",
        "model_usage",
        "model_binding_snapshot",
        "model_rate_limit_window",
        "resource_definition",
        "resource_version",
        "release",
        "runtime_bundle",
        "deployment",
        "agent_version",
        "agent_snapshot",
        "agent_definition",
        "agent_binding",
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
        "model_usage",
        "model_binding_snapshot",
        "budget_reservation",
        "model_rate_limit_window",
        "resource_definition",
        "resource_version",
        "release",
        "runtime_bundle",
        "deployment",
    ):
        assert f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY" in sql
        assert f"CREATE POLICY tenant_isolation ON {table_name}" in sql

    assert sql.count("WITH CHECK") == 19
    assert sql.count("current_setting('app.current_tenant_id', true)") == 38


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


def test_offline_upgrade_contains_immutable_model_binding_snapshots() -> None:
    sql = render_upgrade_sql()

    assert "CREATE TABLE model_binding_snapshot" in sql
    assert "uq_resource_version__tenant_id_id" in sql
    assert "uq_model_binding_snapshot__model_config_version_id" in sql
    assert "fk_model_binding_snapshot__config_definition" in sql
    assert "fk_model_binding_snapshot__config_version" in sql
    assert "fk_model_binding_snapshot__provider_definition" in sql
    assert "ck_model_binding_snapshot__snapshot_hash" in sql
    assert "INSERT INTO model_binding_snapshot" in sql


def test_offline_upgrade_contains_model_gateway_admission_tables() -> None:
    sql = render_upgrade_sql()

    assert "CREATE TABLE budget_reservation" in sql
    assert "uq_budget_reservation__tenant_run_idempotency" in sql
    assert "ck_budget_reservation__consumed_status" in sql
    assert "CREATE TABLE model_rate_limit_window" in sql
    assert "pk_model_rate_limit_window" in sql
    assert "ck_model_rate_limit_window__request_count" in sql


def test_offline_upgrade_contains_agent_draft_bindings_and_permissions() -> None:
    sql = render_upgrade_sql()

    assert "CREATE TABLE agent_definition" in sql
    assert "CREATE TABLE agent_binding" in sql
    assert "uq_agent_definition__tenant_code_active" in sql
    assert "uq_agent_binding__agent_resource_role" in sql
    assert "uq_agent_binding__agent_model_role" in sql
    assert "ck_agent_binding__version_policy_version" in sql
    assert "ck_agent_binding__routing_configuration" in sql
    assert "SELECT tenant_id, id, 'agent', 'create' FROM role" in sql


def test_offline_upgrade_contains_immutable_agent_snapshots() -> None:
    sql = render_upgrade_sql()

    assert "CREATE TABLE agent_version" in sql
    assert "CREATE TABLE agent_snapshot" in sql
    assert "uq_agent_version__agent_id_version_no" in sql
    assert "uq_agent_snapshot__agent_version_id" in sql
    assert "fk_agent_snapshot__tenant_version__agent_version" in sql
    assert "ix_agent_snapshot__content_json_gin" in sql
    assert "reject_agent_release_fact_mutation" in sql
    assert "trg_agent_version__immutable" in sql
    assert "trg_agent_snapshot__immutable" in sql


def test_offline_upgrade_contains_release_and_runtime_bundle_state() -> None:
    sql = render_upgrade_sql()

    assert "CREATE TABLE release" in sql
    assert "CREATE TABLE runtime_bundle" in sql
    assert "uq_release__workflow_id" in sql
    assert "fk_release__tenant_snapshot__agent_snapshot" in sql
    assert "uq_runtime_bundle__snapshot_runtime_compiler_hash" in sql
    assert "fk_runtime_bundle__tenant_snapshot__agent_snapshot" in sql
    assert "ck_runtime_bundle__scan_status" in sql
    assert "activation_fencing_token BIGINT GENERATED BY DEFAULT AS IDENTITY" in sql
    assert "release_kind VARCHAR(20) DEFAULT 'PUBLISH' NOT NULL" in sql
    assert "requested_snapshot_id UUID" in sql
    assert "ck_release__release_source" in sql
    assert "fk_release__tenant_requested_snapshot__agent_snapshot" in sql
    assert "CREATE TABLE deployment" in sql
    assert "uq_deployment__release_runtime_target" in sql
    assert "uq_deployment__tenant_agent_runtime_target_active" in sql
    assert "WHERE status = 'ACTIVE'" in sql
    assert "fk_deployment__tenant_release_agent_snapshot__release" in sql
    assert "fk_deployment__tenant_snapshot_bundle__runtime_bundle" in sql
    assert "fk_agent_definition__tenant_active_deployment__deployment" in sql

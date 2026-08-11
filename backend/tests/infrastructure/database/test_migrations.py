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
        "artifact",
        "approval_request",
        "approval_decision",
        "execution_ticket",
        "agent_run",
        "run_attempt",
        "run_event",
        "run_event_counter",
        "sandbox_instance",
        "sandbox_lease",
        "skill_supply_chain_scan",
        "mcp_capability_discovery",
        "approval_request",
        "approval_decision",
        "workspace",
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
        "chat_message",
        "chat_session",
        "agent_run",
        "run_attempt",
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
        "chat_message",
        "chat_session",
        "run_event",
        "run_event_counter",
        "sandbox_instance",
        "sandbox_lease",
        "workspace",
        "artifact",
        "audit_log",
        "skill_supply_chain_scan",
        "mcp_capability_discovery",
        "execution_ticket",
    ):
        assert f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY" in sql
        assert f"CREATE POLICY tenant_isolation ON {table_name}" in sql

    assert sql.count("WITH CHECK") == 35
    assert sql.count("current_setting('app.current_tenant_id', true)") == 70


def test_offline_upgrade_contains_approval_state_guards_and_permissions() -> None:
    sql = render_upgrade_sql()

    assert "CREATE TABLE approval_request" in sql
    assert "CREATE TABLE approval_decision" in sql
    assert "uq_approval_request__tenant_run_attempt_tool_call" in sql
    assert "uq_approval_decision__approval_id" in sql
    assert "trg_approval_request__guard" in sql
    assert "CREATE TABLE execution_ticket" in sql
    assert "uq_execution_ticket__approval_id" in sql
    assert "trg_execution_ticket__guard" in sql
    assert "trg_approval_decision__guard" in sql
    for action in ("read", "list", "approve"):
        assert f"SELECT tenant_id, id, 'approval', '{action}' FROM role" in sql


def test_offline_upgrade_contains_audit_query_retention_and_permissions() -> None:
    sql = render_upgrade_sql()

    assert "0029_audit_query_retention" in sql
    assert "ALTER TABLE audit_log ADD COLUMN run_id UUID" in sql
    assert "ix_audit_log__tenant_created_id" in sql
    assert "ix_audit_log__tenant_run_created_id" in sql
    assert "ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY" in sql
    assert "ALTER TABLE audit_log FORCE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY tenant_isolation ON audit_log" in sql
    assert "current_setting('app.platform_context', true) = 'true'" in sql
    assert "trg_audit_log__guard" in sql
    assert "audit log facts are immutable and retained" in sql
    assert "SELECT tenant_id, id, 'audit', 'list' FROM role" in sql


def test_offline_upgrade_contains_artifact_quarantine_and_scan_guards() -> None:
    sql = render_upgrade_sql()

    assert "CREATE TABLE artifact" in sql
    assert "fk_artifact__tenant_owner__tenant_member" in sql
    assert "fk_artifact__tenant_workspace_run_owner__workspace" in sql
    assert "ck_artifact__trusted_object_status" in sql
    assert "ck_artifact__scan_result_status" in sql
    assert "trg_artifact__guard" in sql
    assert "artifact.scan_requested.v1" not in sql
    assert "SELECT tenant_id, id, 'artifact', 'create' FROM role" in sql
    assert "SELECT tenant_id, id, 'artifact', 'read' FROM role" in sql


def test_offline_upgrade_contains_artifact_download_and_delete_lifecycle() -> None:
    sql = render_upgrade_sql()

    assert "0024_artifact_download_delete" in sql
    assert "status IN ('DELETING','DELETED')" in sql
    assert "SELECT tenant_id, id, 'artifact', 'download' FROM role" in sql
    assert "SELECT tenant_id, id, 'artifact', 'delete' FROM role" in sql


def test_offline_upgrade_contains_skill_scan_evidence_and_permissions() -> None:
    sql = render_upgrade_sql()

    assert "CREATE TABLE skill_supply_chain_scan" in sql
    assert "fk_skill_supply_chain_scan__tenant_definition" in sql
    assert "fk_skill_supply_chain_scan__tenant_published_version" in sql
    assert "trg_skill_supply_chain_scan__guard" in sql
    assert "ck_skill_supply_chain_scan__published_version_status" in sql
    for action in (
        "create",
        "read",
        "list",
        "update",
        "delete",
        "publish",
        "rollback",
        "disable",
    ):
        assert f"SELECT tenant_id, id, 'skill', '{action}' FROM role" in sql


def test_offline_upgrade_contains_mcp_discovery_evidence_and_permissions() -> None:
    sql = render_upgrade_sql()

    assert "CREATE TABLE mcp_capability_discovery" in sql
    assert "fk_mcp_capability_discovery__tenant_definition" in sql
    assert "fk_mcp_capability_discovery__tenant_published_version" in sql
    assert "fk_mcp_capability_discovery__tenant_source" in sql
    assert "trg_mcp_capability_discovery__guard" in sql
    assert "ck_mcp_capability_discovery__published_version_status" in sql
    for action in (
        "create",
        "read",
        "list",
        "update",
        "delete",
        "publish",
        "rollback",
        "disable",
        "execute",
    ):
        assert f"SELECT tenant_id, id, 'mcp', '{action}' FROM role" in sql


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


def test_offline_upgrade_contains_user_owned_chat_sessions() -> None:
    sql = render_upgrade_sql()

    assert "CREATE TABLE chat_session" in sql
    assert "uq_deployment__tenant_id_agent_id" in sql
    assert "fk_chat_session__tenant_user__tenant_member" in sql
    assert "fk_chat_session__tenant_deployment_agent__deployment" in sql
    assert "ck_chat_session__lifecycle_timestamps" in sql
    assert "ix_chat_session__tenant_user_updated_at" in sql
    assert "SELECT tenant_id, id, 'session', 'create' FROM role" in sql


def test_offline_upgrade_contains_immutable_message_chains() -> None:
    sql = render_upgrade_sql()

    assert "CREATE TABLE chat_message" in sql
    assert "fk_chat_message__tenant_session__chat_session" in sql
    assert "fk_chat_message__tenant_parent_session__chat_message" in sql
    assert "fk_chat_session__tenant_cursor_session__chat_message" in sql
    assert "uq_chat_message__branch_parent" in sql
    assert "NULLS NOT DISTINCT" in sql
    assert "trg_chat_message__immutable" in sql
    assert "SELECT tenant_id, id, 'message', 'list' FROM role" in sql


def test_offline_upgrade_contains_run_identity_state_and_guards() -> None:
    sql = render_upgrade_sql()

    assert "CREATE TABLE agent_run" in sql
    assert "CREATE TABLE run_attempt" in sql
    assert "uq_agent_run__active_session_branch" in sql
    assert "uq_agent_run__tenant_created_by_idempotency" in sql
    assert "fk_agent_run__tenant_deployment_agent_snapshot__deployment" in sql
    assert "fk_chat_message__tenant_source_run_session__agent_run" in sql
    assert "guard_agent_run_mutation" in sql
    assert "trg_run_attempt__guard" in sql
    assert "SELECT tenant_id, id, 'run', 'create' FROM role" in sql
    assert "SELECT tenant_id, id, 'run', 'cancel' FROM role" in sql
    assert "SELECT tenant_id, id, 'run', 'retry' FROM role" in sql
    assert "temporal_run_id VARCHAR(255)" in sql
    assert "workflow_start_outcome VARCHAR(24)" in sql
    assert "workflow_started_at TIMESTAMP WITH TIME ZONE" in sql
    assert "cancelling_at TIMESTAMP WITH TIME ZONE" in sql
    assert "ck_agent_run__workflow_start_mapping" in sql
    assert "uq_agent_run__tenant_workflow_id" in sql
    assert "ix_agent_run__tenant_status_cancelling_at" in sql


def test_offline_upgrade_contains_immutable_run_event_store() -> None:
    sql = render_upgrade_sql()

    assert "CREATE TABLE run_event (" in sql
    assert "CREATE TABLE run_event_counter (" in sql
    assert "PARTITION BY" not in sql
    assert "pk_run_event" in sql
    assert "pk_run_event_counter" in sql
    assert "uq_run_event__run_sequence" in sql
    assert "uq_run_event__run_attempt_source" in sql
    assert "fk_run_event__tenant_run_session__agent_run" in sql
    assert "fk_run_event_counter__tenant_run__agent_run" in sql
    assert "ck_run_event__payload_size" in sql
    assert "octet_length(payload_json::text) <= 262144" in sql
    assert "ix_run_event__tenant_run_sequence" in sql
    assert "ix_run_event__tenant_type_recorded_at" in sql
    assert "trg_run_event__immutable" in sql
    assert "reject_run_event_mutation" in sql


def test_offline_upgrade_contains_fenced_sandbox_lifecycle() -> None:
    sql = render_upgrade_sql()

    assert "CREATE TABLE sandbox_instance" in sql
    assert "CREATE TABLE sandbox_lease" in sql
    assert "uq_sandbox_instance__tenant_run_attempt_policy" in sql
    assert "uq_sandbox_lease__sandbox_active" in sql
    assert "fk_sandbox_instance__tenant_run_session__agent_run" in sql
    assert "fk_sandbox_lease__tenant_sandbox__sandbox_instance" in sql
    assert "trg_sandbox_instance__guard" in sql
    assert "trg_sandbox_lease__guard" in sql
    assert "sandbox_instance lifecycle transition is not allowed" in sql
    assert "sandbox_lease immutable fields cannot change" in sql


def test_offline_upgrade_contains_workspace_identity_quota_and_guards() -> None:
    sql = render_upgrade_sql()

    assert "CREATE TABLE workspace" in sql
    assert "uq_workspace__tenant_uri" in sql
    assert "uq_workspace__tenant_run" in sql
    assert "fk_workspace__tenant_run_session__agent_run" in sql
    assert "fk_sandbox_instance__tenant_workspace__workspace" in sql
    assert "INSERT INTO workspace" in sql
    assert "trg_workspace__guard" in sql
    assert "workspace immutable identity or quota cannot change" in sql

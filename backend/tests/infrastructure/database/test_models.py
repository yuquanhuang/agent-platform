"""IAM metadata constraint and index tests."""

from sqlalchemy import JSON, CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from packages.infrastructure.database.public import Base


def constraint_names(table_name: str, constraint_type: type[object]) -> set[str]:
    table = Base.metadata.tables[table_name]
    return {
        str(constraint.name)
        for constraint in table.constraints
        if isinstance(constraint, constraint_type)
    }


def index_names(table_name: str) -> set[str]:
    return {str(index.name) for index in Base.metadata.tables[table_name].indexes}


def test_iam_metadata_contains_foundation_and_rbac_tables() -> None:
    assert {
        "app_user",
        "approval_decision",
        "approval_request",
        "agent_binding",
        "artifact",
        "agent_definition",
        "agent_snapshot",
        "agent_run",
        "agent_version",
        "audit_log",
        "budget_reservation",
        "idempotency_record",
        "mcp_capability_discovery",
        "model_binding_snapshot",
        "model_rate_limit_window",
        "operation_record",
        "outbox_event",
        "resource_definition",
        "resource_version",
        "release",
        "runtime_bundle",
        "run_attempt",
        "run_event",
        "run_event_counter",
        "sandbox_instance",
        "sandbox_lease",
        "skill_supply_chain_scan",
        "workspace",
        "deployment",
        "execution_ticket",
        "chat_message",
        "chat_session",
        "role",
        "role_binding",
        "role_permission",
        "tenant",
        "tenant_member",
    } <= set(Base.metadata.tables)


def test_tenant_scoped_tables_require_tenant_id_and_baseline_indexes() -> None:
    expected_tenant_indexes = {
        "tenant_member": "ix_tenant_member__tenant_id_id",
        "role": "uq_role__tenant_id_id",
        "role_binding": "ix_role_binding__tenant_id_id",
        "role_permission": "ix_role_permission__tenant_id_role_id",
        "operation_record": "ix_operation_record__tenant_id_created_at",
        "outbox_event": "ix_outbox_event__tenant_id_id",
        "resource_definition": "uq_resource_definition__tenant_id_id",
        "resource_version": "ix_resource_version__tenant_definition_published_at",
        "model_usage": "ix_model_usage__tenant_id_run_id",
        "model_binding_snapshot": (
            "ix_model_binding_snapshot__tenant_model_config_definition"
        ),
        "mcp_capability_discovery": (
            "ix_mcp_capability_discovery__tenant_definition_discovered_at"
        ),
        "budget_reservation": ("ix_budget_reservation__tenant_run_status_expires"),
        "model_rate_limit_window": ("ix_model_rate_limit_window__window_started_at"),
        "agent_definition": "uq_agent_definition__tenant_id_id",
        "agent_binding": "ix_agent_binding__tenant_agent",
        "agent_version": "uq_agent_version__tenant_id_id",
        "agent_snapshot": "uq_agent_snapshot__tenant_id_id",
        "release": "uq_release__tenant_id_id",
        "runtime_bundle": "uq_runtime_bundle__tenant_id_id",
        "deployment": "uq_deployment__tenant_id_id",
        "chat_message": "uq_chat_message__tenant_id_session_id",
        "chat_session": "uq_chat_session__tenant_id_id",
        "agent_run": "uq_agent_run__tenant_id_id",
        "run_attempt": "uq_run_attempt__tenant_id_id",
        "run_event": "ix_run_event__tenant_run_sequence",
        "run_event_counter": "ix_run_event_counter__tenant_run",
        "sandbox_instance": "uq_sandbox_instance__tenant_id_id",
        "sandbox_lease": "uq_sandbox_lease__tenant_id_id",
        "workspace": "uq_workspace__tenant_id_id",
        "artifact": "ix_artifact__tenant_owner_created_at",
        "approval_request": "ix_approval_request__tenant_status_expires_at",
        "approval_decision": "uq_approval_decision__tenant_id_id",
        "execution_ticket": "ix_execution_ticket__tenant_run_expires_at",
        "skill_supply_chain_scan": (
            "ix_skill_supply_chain_scan__tenant_definition_scanned_at"
        ),
    }

    for table_name, expected_index in expected_tenant_indexes.items():
        table = Base.metadata.tables[table_name]
        assert table.c.tenant_id.nullable is False
        assert expected_index in index_names(table_name) | constraint_names(
            table_name, UniqueConstraint
        )


def test_execution_ticket_freezes_bindings_and_single_use_state() -> None:
    table = Base.metadata.tables["execution_ticket"]

    assert {
        "tenant_id",
        "approval_id",
        "run_id",
        "execution_attempt",
        "requester_id",
        "tool_name",
        "tool_schema_hash",
        "parameter_digest",
        "policy_version",
        "deployment_id",
        "nonce_hash",
        "expires_at",
        "single_use",
        "consumed_at",
    } <= set(table.c.keys())
    assert "uq_execution_ticket__approval_id" in constraint_names(
        "execution_ticket", UniqueConstraint
    )
    assert {
        "fk_execution_ticket__tenant_approval__approval_request",
        "fk_execution_ticket__tenant_run__agent_run",
        "fk_execution_ticket__tenant_requester__tenant_member",
        "fk_execution_ticket__tenant_deployment__deployment",
    } <= constraint_names("execution_ticket", ForeignKeyConstraint)


def test_model_usage_metadata_has_normalized_usage_fields_and_indexes() -> None:
    table = Base.metadata.tables["model_usage"]
    assert {
        "tenant_id",
        "run_id",
        "provider",
        "model",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "token_estimated",
        "cost_amount",
        "cost_currency",
    } <= set(table.c.keys())
    assert {
        "ix_model_usage__tenant_id_run_id",
        "ix_model_usage__tenant_id_finished_at",
    } <= index_names("model_usage")


def test_model_binding_snapshot_freezes_provider_and_model_configuration() -> None:
    table = Base.metadata.tables["model_binding_snapshot"]

    assert {
        "tenant_id",
        "model_config_definition_id",
        "model_config_version_id",
        "provider_definition_id",
        "provider_type",
        "base_url",
        "secret_ref",
        "provider_timeout_seconds",
        "model_id",
        "capabilities_json",
        "default_parameters_json",
        "max_context_tokens",
        "rate_limit_rpm",
        "snapshot_hash",
    } <= set(table.c.keys())
    assert "uq_model_binding_snapshot__model_config_version_id" in constraint_names(
        "model_binding_snapshot", UniqueConstraint
    )
    assert {
        "fk_model_binding_snapshot__config_definition",
        "fk_model_binding_snapshot__config_version",
        "fk_model_binding_snapshot__provider_definition",
    } <= constraint_names("model_binding_snapshot", ForeignKeyConstraint)


def test_model_gateway_admission_metadata_is_tenant_scoped() -> None:
    reservation = Base.metadata.tables["budget_reservation"]
    window = Base.metadata.tables["model_rate_limit_window"]

    assert {
        "tenant_id",
        "run_id",
        "user_id",
        "agent_id",
        "model_binding_id",
        "idempotency_key",
        "reserved_tokens",
        "consumed_tokens",
        "status",
        "expires_at",
        "finished_at",
    } <= set(reservation.c.keys())
    assert "uq_budget_reservation__tenant_run_idempotency" in constraint_names(
        "budget_reservation", UniqueConstraint
    )
    assert {column.name for column in window.primary_key.columns} == {
        "tenant_id",
        "model_binding_id",
        "provider",
        "window_started_at",
    }
    assert window.c.request_count.nullable is False


def test_skill_scan_metadata_is_immutable_and_version_bindable() -> None:
    table = Base.metadata.tables["skill_supply_chain_scan"]

    assert {
        "tenant_id",
        "definition_id",
        "draft_resource_version",
        "content_hash",
        "scanner_name",
        "scanner_version",
        "policy_version",
        "status",
        "findings_json",
        "report_hash",
        "sbom_json",
        "sbom_hash",
        "signature_status",
        "provenance_status",
        "scanned_by",
        "published_version_id",
    } <= set(table.c.keys())
    assert {
        "fk_skill_supply_chain_scan__tenant_definition",
        "fk_skill_supply_chain_scan__tenant_published_version",
    } <= constraint_names("skill_supply_chain_scan", ForeignKeyConstraint)
    assert "uq_skill_supply_chain_scan__published_version_id" in constraint_names(
        "skill_supply_chain_scan", UniqueConstraint
    )


def test_mcp_discovery_metadata_is_immutable_and_version_bindable() -> None:
    table = Base.metadata.tables["mcp_capability_discovery"]

    assert {
        "tenant_id",
        "definition_id",
        "operation_id",
        "draft_resource_version",
        "content_hash",
        "status",
        "protocol_version",
        "server_name",
        "server_version",
        "tools_json",
        "capability_hash",
        "findings_json",
        "discovered_by",
        "source_discovery_id",
        "published_version_id",
    } <= set(table.c.keys())
    assert {
        "fk_mcp_capability_discovery__tenant_definition",
        "fk_mcp_capability_discovery__tenant_published_version",
        "fk_mcp_capability_discovery__tenant_source",
    } <= constraint_names("mcp_capability_discovery", ForeignKeyConstraint)
    assert {
        "uq_mcp_capability_discovery__operation_id",
        "uq_mcp_capability_discovery__published_version_id",
    } <= constraint_names("mcp_capability_discovery", UniqueConstraint)


def test_agent_draft_metadata_has_cas_bindings_and_route_uniqueness() -> None:
    definition = Base.metadata.tables["agent_definition"]
    binding = Base.metadata.tables["agent_binding"]

    assert {
        "tenant_id",
        "code",
        "runtime_type",
        "visibility",
        "tags_json",
        "owner_user_id",
        "status",
        "active_deployment_id",
        "resource_version",
        "deleted_at",
    } <= set(definition.c.keys())
    assert {
        "tenant_id",
        "agent_id",
        "resource_type",
        "resource_id",
        "version_policy",
        "fixed_version_id",
        "binding_role",
        "configuration_json",
        "configuration_schema_version",
    } <= set(binding.c.keys())
    assert {
        "uq_agent_binding__agent_resource_role",
        "uq_agent_binding__agent_model_role",
        "ix_agent_binding__tenant_resource",
    } <= index_names("agent_binding")
    configuration_type = binding.c.configuration_json.type
    assert isinstance(configuration_type, JSON)
    assert configuration_type.none_as_null is True
    assert "fk_agent_binding__tenant_agent__agent_definition" in constraint_names(
        "agent_binding", ForeignKeyConstraint
    )


def test_chat_session_metadata_pins_user_agent_and_deployment() -> None:
    table = Base.metadata.tables["chat_session"]

    assert {
        "tenant_id",
        "user_id",
        "agent_id",
        "default_deployment_id",
        "title",
        "status",
        "metadata_json",
        "metadata_schema_version",
        "resource_version",
        "archived_at",
        "deleted_at",
    } <= set(table.c.keys())
    assert {
        "fk_chat_session__tenant_user__tenant_member",
        "fk_chat_session__tenant_agent__agent_definition",
        "fk_chat_session__tenant_deployment_agent__deployment",
        "fk_chat_session__tenant_cursor_session__chat_message",
    } <= constraint_names("chat_session", ForeignKeyConstraint)
    assert {
        "ix_chat_session__tenant_user_updated_at",
        "ix_chat_session__tenant_agent_created_at",
    } <= index_names("chat_session")


def test_artifact_metadata_separates_quarantine_from_trusted_storage() -> None:
    table = Base.metadata.tables["artifact"]

    assert {
        "tenant_id",
        "workspace_id",
        "run_id",
        "owner_user_id",
        "quarantine_object_uri",
        "object_uri",
        "content_hash",
        "size_bytes",
        "content_type",
        "status",
        "scan_result_json",
        "upload_expires_at",
        "expires_at",
    } <= set(table.c.keys())
    assert {
        "fk_artifact__tenant_owner__tenant_member",
        "fk_artifact__tenant_workspace_run_owner__workspace",
    } <= constraint_names("artifact", ForeignKeyConstraint)
    assert {
        "ck_artifact__trusted_object_status",
        "ck_artifact__scan_result_status",
        "ck_artifact__workspace_run_binding",
    } <= constraint_names("artifact", CheckConstraint)
    scan_result_type = table.c.scan_result_json.type
    assert isinstance(scan_result_type, JSON)
    assert scan_result_type.none_as_null is True


def test_chat_message_metadata_enforces_parent_chain_and_branch_order() -> None:
    table = Base.metadata.tables["chat_message"]

    assert {
        "tenant_id",
        "session_id",
        "branch_id",
        "parent_message_id",
        "role",
        "content_parts_json",
        "content_schema_version",
        "source_run_id",
        "created_by",
    } <= set(table.c.keys())
    assert {
        "fk_chat_message__tenant_session__chat_session",
        "fk_chat_message__tenant_parent_session__chat_message",
        "fk_chat_message__tenant_source_run_session__agent_run",
    } <= constraint_names("chat_message", ForeignKeyConstraint)


def test_agent_run_metadata_pins_messages_deployment_snapshot_and_inputs() -> None:
    table = Base.metadata.tables["agent_run"]

    assert {
        "tenant_id",
        "session_id",
        "branch_id",
        "user_message_id",
        "assistant_message_id",
        "agent_id",
        "snapshot_id",
        "deployment_id",
        "status",
        "idempotency_key",
        "timeout_seconds",
        "token_budget",
        "cost_budget_amount",
        "cost_budget_currency",
        "workflow_id",
        "temporal_run_id",
        "workflow_start_outcome",
        "workflow_started_at",
        "cancelling_at",
    } <= set(table.c.keys())
    assert {
        "fk_agent_run__tenant_session__chat_session",
        "fk_agent_run__tenant_user_message_session__chat_message",
        "fk_agent_run__tenant_assistant_message_session__chat_message",
        "fk_agent_run__tenant_deployment_agent_snapshot__deployment",
        "fk_agent_run__tenant_snapshot__agent_snapshot",
    } <= constraint_names("agent_run", ForeignKeyConstraint)
    assert "uq_agent_run__active_session_branch" in index_names("agent_run")
    assert "uq_agent_run__tenant_workflow_id" in constraint_names(
        "agent_run", UniqueConstraint
    )
    assert "ix_agent_run__tenant_status_cancelling_at" in index_names("agent_run")


def test_run_attempt_metadata_is_tenant_scoped_and_fenced() -> None:
    table = Base.metadata.tables["run_attempt"]

    assert {
        "tenant_id",
        "run_id",
        "attempt_no",
        "fencing_token_hash",
        "status",
    } <= set(table.c.keys())
    assert "created_by" not in table.c
    assert "fk_run_attempt__tenant_run__agent_run" in constraint_names(
        "run_attempt", ForeignKeyConstraint
    )
    assert {
        "ix_chat_message__tenant_session_branch_created_at",
        "ix_chat_message__tenant_session_parent",
        "uq_chat_message__branch_parent",
    } <= index_names("chat_message")


def test_run_event_metadata_enforces_identity_payload_and_replay_indexes() -> None:
    event = Base.metadata.tables["run_event"]
    counter = Base.metadata.tables["run_event_counter"]

    assert {
        "id",
        "tenant_id",
        "run_id",
        "session_id",
        "sequence_no",
        "source_event_id",
        "execution_attempt",
        "schema_version",
        "event_type",
        "payload_version",
        "payload_json",
        "occurred_at",
        "recorded_at",
        "trace_id",
    } == set(event.c.keys())
    assert {column.name for column in event.primary_key.columns} == {"id"}
    assert {
        "uq_run_event__run_sequence",
        "uq_run_event__run_attempt_source",
    } <= constraint_names("run_event", UniqueConstraint)
    assert {
        "fk_run_event__tenant_run_session__agent_run",
    } <= constraint_names("run_event", ForeignKeyConstraint)
    assert {
        "ck_run_event__sequence_no",
        "ck_run_event__execution_attempt",
        "ck_run_event__schema_version",
        "ck_run_event__payload_version",
        "ck_run_event__event_type",
        "ck_run_event__payload_json",
        "ck_run_event__payload_size",
    } <= constraint_names("run_event", CheckConstraint)
    assert {
        "ix_run_event__tenant_run_sequence",
        "ix_run_event__tenant_type_recorded_at",
    } <= index_names("run_event")

    assert {column.name for column in counter.primary_key.columns} == {"run_id"}
    assert set(counter.c.keys()) == {
        "run_id",
        "tenant_id",
        "next_sequence_no",
    }
    assert "fk_run_event_counter__tenant_run__agent_run" in constraint_names(
        "run_event_counter", ForeignKeyConstraint
    )
    assert "ck_run_event_counter__next_sequence_no" in constraint_names(
        "run_event_counter", CheckConstraint
    )


def test_sandbox_metadata_pins_immutable_policy_bundle_and_fenced_lease() -> None:
    instance = Base.metadata.tables["sandbox_instance"]
    lease = Base.metadata.tables["sandbox_lease"]

    assert {
        "tenant_id",
        "user_id",
        "session_id",
        "run_id",
        "execution_attempt",
        "scope",
        "image_digest",
        "policy_ref",
        "policy_hash",
        "policy_schema_version",
        "policy_json",
        "bundle_ref",
        "bundle_hash",
        "workspace_uri",
        "runtime_target_id",
        "status",
        "provider_ref",
        "lease_expires_at",
        "provision_operation_id",
        "terminated_at",
        "failure_code",
    } <= set(instance.c.keys())
    assert {
        "tenant_id",
        "sandbox_id",
        "holder_run_id",
        "execution_attempt",
        "fencing_token_hash",
        "acquired_at",
        "expires_at",
        "released_at",
    } <= set(lease.c.keys())
    assert {
        "fk_sandbox_instance__tenant_run_session__agent_run",
        "fk_sandbox_instance__tenant_user__tenant_member",
        "fk_sandbox_instance__tenant_workspace__workspace",
    } <= constraint_names("sandbox_instance", ForeignKeyConstraint)
    assert {
        "fk_sandbox_lease__tenant_sandbox__sandbox_instance",
        "fk_sandbox_lease__tenant_run__agent_run",
    } <= constraint_names("sandbox_lease", ForeignKeyConstraint)
    assert "uq_sandbox_instance__tenant_run_attempt_policy" in constraint_names(
        "sandbox_instance", UniqueConstraint
    )
    assert "uq_sandbox_lease__sandbox_active" in index_names("sandbox_lease")


def test_workspace_metadata_pins_identity_retention_and_capacity() -> None:
    workspace = Base.metadata.tables["workspace"]

    assert {
        "tenant_id",
        "user_id",
        "session_id",
        "run_id",
        "uri",
        "quota_bytes",
        "used_bytes",
        "max_files",
        "file_count",
        "max_file_bytes",
        "status",
        "created_at",
        "updated_at",
        "expires_at",
    } <= set(workspace.c.keys())
    assert {
        "fk_workspace__tenant_run_session__agent_run",
        "fk_workspace__tenant_user__tenant_member",
    } <= constraint_names("workspace", ForeignKeyConstraint)
    assert {
        "uq_workspace__tenant_uri",
        "uq_workspace__tenant_run",
    } <= constraint_names("workspace", UniqueConstraint)
    assert {
        "ix_workspace__tenant_status_expires_at",
        "ix_workspace__tenant_session_created_at",
    } <= index_names("workspace")


def test_agent_snapshot_metadata_is_versioned_and_immutable_by_shape() -> None:
    version = Base.metadata.tables["agent_version"]
    snapshot = Base.metadata.tables["agent_snapshot"]

    assert {
        "tenant_id",
        "agent_id",
        "version_no",
        "created_from_version_id",
        "release_note",
        "created_by",
    } <= set(version.c.keys())
    assert {
        "tenant_id",
        "agent_version_id",
        "schema_version",
        "content_json",
        "content_hash",
        "compiler_input_hash",
        "created_by",
    } <= set(snapshot.c.keys())
    assert "uq_agent_version__agent_id_version_no" in constraint_names(
        "agent_version", UniqueConstraint
    )
    assert "uq_agent_snapshot__agent_version_id" in constraint_names(
        "agent_snapshot", UniqueConstraint
    )
    assert {
        "fk_agent_version__tenant_agent__agent_definition",
        "fk_agent_version__tenant_created_from__agent_version",
    } <= constraint_names("agent_version", ForeignKeyConstraint)
    assert "fk_agent_snapshot__tenant_version__agent_version" in constraint_names(
        "agent_snapshot", ForeignKeyConstraint
    )


def test_release_and_runtime_bundle_metadata_support_durable_workflow_state() -> None:
    release = Base.metadata.tables["release"]
    bundle = Base.metadata.tables["runtime_bundle"]

    assert {
        "tenant_id",
        "agent_id",
        "requested_by",
        "operation_id",
        "release_kind",
        "expected_agent_version",
        "requested_snapshot_id",
        "runtime_targets_json",
        "run_smoke_test",
        "activate_on_success",
        "status",
        "workflow_id",
        "snapshot_id",
        "deployment_ids_json",
        "error_code",
        "error_detail_json",
        "started_at",
        "finished_at",
    } <= set(release.c.keys())
    assert {
        "tenant_id",
        "snapshot_id",
        "runtime_type",
        "compiler_name",
        "compiler_version",
        "manifest_schema_version",
        "manifest_json",
        "content_hash",
        "object_uri",
        "size_bytes",
        "signature_ref",
        "sbom_ref",
        "scan_status",
    } <= set(bundle.c.keys())
    assert "fk_release__tenant_snapshot__agent_snapshot" in constraint_names(
        "release", ForeignKeyConstraint
    )
    assert "fk_runtime_bundle__tenant_snapshot__agent_snapshot" in constraint_names(
        "runtime_bundle", ForeignKeyConstraint
    )
    assert "uq_runtime_bundle__snapshot_runtime_compiler_hash" in constraint_names(
        "runtime_bundle", UniqueConstraint
    )


def test_deployment_metadata_enforces_history_fencing_and_single_active() -> None:
    deployment = Base.metadata.tables["deployment"]
    release = Base.metadata.tables["release"]

    assert {
        "tenant_id",
        "release_id",
        "agent_id",
        "snapshot_id",
        "bundle_id",
        "runtime_target_id",
        "status",
        "compatibility_hash",
        "activation_fencing_token",
        "activated_at",
        "retired_at",
    } <= set(deployment.c.keys())
    assert "activation_fencing_token" in release.c
    assert "uq_deployment__tenant_agent_runtime_target_active" in index_names(
        "deployment"
    )
    assert "uq_deployment__release_runtime_target" in constraint_names(
        "deployment", UniqueConstraint
    )
    assert {
        "fk_deployment__tenant_release_agent_snapshot__release",
        "fk_deployment__tenant_agent__agent_definition",
        "fk_deployment__tenant_snapshot__agent_snapshot",
        "fk_deployment__tenant_snapshot_bundle__runtime_bundle",
    } <= constraint_names("deployment", ForeignKeyConstraint)
    assert (
        "fk_agent_definition__tenant_active_deployment__deployment"
        in constraint_names("agent_definition", ForeignKeyConstraint)
    )


def test_membership_has_authorization_and_resource_versions() -> None:
    member = Base.metadata.tables["tenant_member"]

    assert member.c.membership_version.nullable is False
    assert member.c.membership_version.server_default is not None
    assert member.c.resource_version.nullable is False
    assert "ck_tenant_member__membership_version" in constraint_names(
        "tenant_member", CheckConstraint
    )
    assert "uq_tenant_member__tenant_id_user_id" in constraint_names(
        "tenant_member", UniqueConstraint
    )


def test_role_binding_foreign_key_prevents_cross_tenant_role_assignment() -> None:
    foreign_keys = [
        constraint
        for constraint in Base.metadata.tables["role_binding"].constraints
        if isinstance(constraint, ForeignKeyConstraint)
    ]
    composite_role_key = next(
        constraint
        for constraint in foreign_keys
        if constraint.name == "fk_role_binding__tenant_id_role_id__role"
    )

    assert [element.parent.name for element in composite_role_key.elements] == [
        "tenant_id",
        "role_id",
    ]
    assert [element.target_fullname for element in composite_role_key.elements] == [
        "role.tenant_id",
        "role.id",
    ]


def test_role_binding_has_subject_and_resource_lookup_indexes() -> None:
    assert {
        "ix_role_binding__tenant_id_subject_type_subject_id",
        "ix_role_binding__tenant_id_resource_type_resource_id",
    } <= index_names("role_binding")


def test_role_permission_is_tenant_scoped_and_bound_to_same_tenant_role() -> None:
    permission = Base.metadata.tables["role_permission"]
    assert permission.c.tenant_id.nullable is False
    assert {column.name for column in permission.primary_key.columns} == {
        "tenant_id",
        "role_id",
        "resource_type",
        "action",
    }
    assert "fk_role_permission__tenant_id_role_id__role" in constraint_names(
        "role_permission", ForeignKeyConstraint
    )


def test_audit_log_has_no_updateable_resource_version() -> None:
    audit_log = Base.metadata.tables["audit_log"]
    assert "resource_version" not in audit_log.c
    assert {
        "ix_audit_log__tenant_id_created_at",
        "ix_audit_log__actor_type_actor_id",
        "ix_audit_log__resource_type_resource_id",
        "ix_audit_log__action",
        "ix_audit_log__tenant_created_id",
        "ix_audit_log__tenant_run_created_id",
    } <= index_names("audit_log")


def test_idempotency_and_operation_metadata_support_replay_and_terminal_state() -> None:
    idempotency = Base.metadata.tables["idempotency_record"]
    operation = Base.metadata.tables["operation_record"]

    assert idempotency.c.tenant_id.nullable is True
    assert idempotency.c.request_hash.nullable is False
    assert idempotency.c.response_body_json.nullable is True
    assert operation.c.tenant_id.nullable is False
    assert operation.c.result_json.nullable is True
    assert "ix_operation_record__tenant_id_resource" in index_names("operation_record")


def test_outbox_metadata_supports_bounded_claim_and_delivery_state() -> None:
    outbox = Base.metadata.tables["outbox_event"]

    assert outbox.c.tenant_id.nullable is False
    assert outbox.c.payload_json.nullable is False
    assert outbox.c.payload_schema_version.nullable is False
    assert "ck_outbox_event__status" in constraint_names(
        "outbox_event", CheckConstraint
    )
    assert "ix_outbox_event__status_next_attempt_at" in index_names("outbox_event")


def test_resource_registry_metadata_has_cas_soft_unique_and_immutable_versions() -> (
    None
):
    definition = Base.metadata.tables["resource_definition"]
    version = Base.metadata.tables["resource_version"]

    assert definition.c.current_draft_json.nullable is False
    assert definition.c.draft_schema_version.nullable is False
    assert definition.c.resource_version.nullable is False
    assert definition.c.deleted_at.nullable is True
    assert {
        "uq_resource_definition__tenant_type_code_active",
        "ix_resource_definition__tenant_type_status_created_at",
    } <= index_names("resource_definition")
    assert "uq_resource_version__definition_id_version_no" in constraint_names(
        "resource_version", UniqueConstraint
    )
    assert "uq_resource_version__definition_content_publish" in index_names(
        "resource_version"
    )
    assert (
        "fk_resource_version__tenant_definition__resource_definition"
        in constraint_names("resource_version", ForeignKeyConstraint)
    )
    assert version.c.content_json.nullable is False
    assert version.c.content_hash.nullable is False
    assert version.c.publication_kind.nullable is False

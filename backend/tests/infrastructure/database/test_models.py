"""IAM metadata constraint and index tests."""

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

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
        "audit_log",
        "idempotency_record",
        "operation_record",
        "outbox_event",
        "resource_definition",
        "resource_version",
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
    }

    for table_name, expected_index in expected_tenant_indexes.items():
        table = Base.metadata.tables[table_name]
        assert table.c.tenant_id.nullable is False
        assert expected_index in index_names(table_name) | constraint_names(
            table_name, UniqueConstraint
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

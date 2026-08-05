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


def test_initial_iam_metadata_contains_only_foundation_tables() -> None:
    assert set(Base.metadata.tables) == {
        "app_user",
        "role",
        "role_binding",
        "tenant",
        "tenant_member",
    }


def test_tenant_scoped_tables_require_tenant_id_and_baseline_indexes() -> None:
    expected_tenant_indexes = {
        "tenant_member": "ix_tenant_member__tenant_id_id",
        "role": "uq_role__tenant_id_id",
        "role_binding": "ix_role_binding__tenant_id_id",
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

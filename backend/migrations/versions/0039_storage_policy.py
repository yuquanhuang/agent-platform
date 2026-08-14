"""Add durable versioned tenant storage capacity policies.

Revision ID: 0039_storage_policy
Revises: 0038_run_admission_queue
Create Date: 2026-08-12
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0039_storage_policy"
down_revision: str | None = "0038_run_admission_queue"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY = (
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)
PERMISSIONS = ("create", "read", "list", "update", "disable")


def upgrade() -> None:
    op.add_column(
        "tenant",
        sa.Column("storage_policy_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_table(
        "storage_policy",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'ACTIVE'"),
            nullable=False,
        ),
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "resource_version",
            sa.BigInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name="status"),
        sa.CheckConstraint("resource_version >= 1", name="resource_version"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_storage_policy__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name="fk_storage_policy__created_by__app_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_storage_policy"),
        sa.UniqueConstraint("tenant_id", name="uq_storage_policy__tenant_id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_storage_policy__tenant_id_id"),
    )
    op.create_index(
        "ix_storage_policy__tenant_status",
        "storage_policy",
        ["tenant_id", "status"],
    )
    op.create_table(
        "storage_policy_version",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("policy_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_no", sa.BigInteger(), nullable=False),
        sa.Column("max_reserved_workspace_bytes", sa.BigInteger(), nullable=True),
        sa.Column("max_reserved_workspaces", sa.BigInteger(), nullable=True),
        sa.Column("max_reserved_artifact_bytes", sa.BigInteger(), nullable=True),
        sa.Column("max_reserved_artifacts", sa.BigInteger(), nullable=True),
        sa.Column("content_hash", sa.String(length=80), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version_no >= 1", name="version_no"),
        sa.CheckConstraint(
            "max_reserved_workspace_bytes IS NOT NULL OR "
            "max_reserved_workspaces IS NOT NULL OR "
            "max_reserved_artifact_bytes IS NOT NULL OR "
            "max_reserved_artifacts IS NOT NULL",
            name="at_least_one_limit",
        ),
        sa.CheckConstraint(
            "max_reserved_workspace_bytes IS NULL OR "
            "max_reserved_workspace_bytes BETWEEN 1 AND 1125899906842624",
            name="workspace_bytes_limit",
        ),
        sa.CheckConstraint(
            "max_reserved_workspaces IS NULL OR "
            "max_reserved_workspaces BETWEEN 1 AND 1000000000",
            name="workspace_count_limit",
        ),
        sa.CheckConstraint(
            "max_reserved_artifact_bytes IS NULL OR "
            "max_reserved_artifact_bytes BETWEEN 1 AND 1125899906842624",
            name="artifact_bytes_limit",
        ),
        sa.CheckConstraint(
            "max_reserved_artifacts IS NULL OR "
            "max_reserved_artifacts BETWEEN 1 AND 1000000000",
            name="artifact_count_limit",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^sha256:[a-f0-9]{64}$'", name="content_hash"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_storage_policy_version__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name="fk_storage_policy_version__created_by__app_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_storage_policy_version"),
        sa.UniqueConstraint(
            "tenant_id",
            "policy_id",
            "version_no",
            name="uq_storage_policy_version__tenant_policy_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "policy_id",
            "id",
            name="uq_storage_policy_version__tenant_policy_id",
        ),
    )
    op.create_index(
        "ix_storage_policy_version__tenant_policy_created",
        "storage_policy_version",
        ["tenant_id", "policy_id", "created_at", "id"],
    )
    op.create_foreign_key(
        "fk_storage_policy_version__tenant_policy",
        "storage_policy_version",
        "storage_policy",
        ["tenant_id", "policy_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_storage_policy__current_version",
        "storage_policy",
        "storage_policy_version",
        ["tenant_id", "id", "current_version_id"],
        ["tenant_id", "policy_id", "id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_tenant__storage_policy_id__storage_policy",
        "tenant",
        "storage_policy",
        ["id", "storage_policy_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    for table_name in ("storage_policy", "storage_policy_version"):
        op.execute(sa.text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"CREATE POLICY tenant_isolation ON {table_name} "
                f"USING ({TENANT_POLICY}) WITH CHECK ({TENANT_POLICY})"
            )
        )
    op.execute(sa.text(_VERSION_IMMUTABILITY_GUARD))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_storage_policy_version__immutable "
            "BEFORE UPDATE OR DELETE ON storage_policy_version FOR EACH ROW "
            "EXECUTE FUNCTION guard_storage_policy_version_immutable()"
        )
    )
    for action in PERMISSIONS:
        op.execute(
            sa.text(
                "DO $$ DECLARE tenant_uuid uuid; BEGIN "
                "FOR tenant_uuid IN SELECT tenant_id FROM role WHERE built_in IS TRUE "
                "AND code = 'tenant_admin' LOOP "
                "PERFORM set_config('app.current_tenant_id', tenant_uuid::text, true); "
                "INSERT INTO role_permission "
                "(tenant_id, role_id, resource_type, action) "
                f"SELECT tenant_id, id, 'storage_policy', '{action}' FROM role "
                "WHERE tenant_id = tenant_uuid AND built_in IS TRUE "
                "AND code = 'tenant_admin' ON CONFLICT DO NOTHING; "
                "END LOOP; END $$"
            )
        )


def downgrade() -> None:
    op.execute(sa.text("""DO $$ DECLARE tenant_uuid uuid; BEGIN
        FOR tenant_uuid IN SELECT tenant_id FROM role WHERE built_in IS TRUE
        AND code = 'tenant_admin' LOOP
            PERFORM set_config('app.current_tenant_id', tenant_uuid::text, true);
            DELETE FROM role_permission WHERE tenant_id = tenant_uuid
            AND resource_type = 'storage_policy';
        END LOOP; END $$"""))
    op.execute(
        sa.text(
            "DROP TRIGGER trg_storage_policy_version__immutable "
            "ON storage_policy_version"
        )
    )
    op.execute(sa.text("DROP FUNCTION guard_storage_policy_version_immutable()"))
    op.drop_constraint(
        "fk_tenant__storage_policy_id__storage_policy", "tenant", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_storage_policy__current_version", "storage_policy", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_storage_policy_version__tenant_policy",
        "storage_policy_version",
        type_="foreignkey",
    )
    for table_name in ("storage_policy_version", "storage_policy"):
        op.execute(sa.text(f"DROP POLICY tenant_isolation ON {table_name}"))
        op.execute(sa.text(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY"))
    op.drop_index(
        "ix_storage_policy_version__tenant_policy_created",
        table_name="storage_policy_version",
    )
    op.drop_table("storage_policy_version")
    op.drop_index("ix_storage_policy__tenant_status", table_name="storage_policy")
    op.drop_table("storage_policy")
    op.drop_column("tenant", "storage_policy_id")


_VERSION_IMMUTABILITY_GUARD = """
CREATE FUNCTION guard_storage_policy_version_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'storage policy versions are immutable';
END;
$$;
"""

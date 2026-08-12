"""Add durable versioned tenant Run quota policies.

Revision ID: 0034_quota_policy
Revises: 0033_runtime_checkpoint
Create Date: 2026-08-11
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034_quota_policy"
down_revision: str | None = "0033_runtime_checkpoint"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY = (
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)
PERMISSIONS = ("create", "read", "list", "update", "disable")


def upgrade() -> None:
    op.create_table(
        "quota_policy",
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
            name="fk_quota_policy__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name="fk_quota_policy__created_by__app_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_quota_policy"),
        sa.UniqueConstraint("tenant_id", name="uq_quota_policy__tenant_id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_quota_policy__tenant_id_id"),
    )
    op.create_index(
        "ix_quota_policy__tenant_status",
        "quota_policy",
        ["tenant_id", "status"],
    )
    op.create_table(
        "quota_policy_version",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("policy_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_no", sa.BigInteger(), nullable=False),
        sa.Column("max_nonterminal_runs_per_tenant", sa.BigInteger(), nullable=True),
        sa.Column("max_nonterminal_runs_per_user", sa.BigInteger(), nullable=True),
        sa.Column("max_nonterminal_runs_per_agent", sa.BigInteger(), nullable=True),
        sa.Column("max_nonterminal_agentscope_runs", sa.BigInteger(), nullable=True),
        sa.Column("max_nonterminal_codex_runs", sa.BigInteger(), nullable=True),
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
            "max_nonterminal_runs_per_tenant IS NOT NULL OR "
            "max_nonterminal_runs_per_user IS NOT NULL OR "
            "max_nonterminal_runs_per_agent IS NOT NULL OR "
            "max_nonterminal_agentscope_runs IS NOT NULL OR "
            "max_nonterminal_codex_runs IS NOT NULL",
            name="at_least_one_limit",
        ),
        sa.CheckConstraint(
            "max_nonterminal_runs_per_tenant IS NULL OR "
            "max_nonterminal_runs_per_tenant BETWEEN 1 AND 1000000",
            name="tenant_limit",
        ),
        sa.CheckConstraint(
            "max_nonterminal_runs_per_user IS NULL OR "
            "max_nonterminal_runs_per_user BETWEEN 1 AND 1000000",
            name="user_limit",
        ),
        sa.CheckConstraint(
            "max_nonterminal_runs_per_agent IS NULL OR "
            "max_nonterminal_runs_per_agent BETWEEN 1 AND 1000000",
            name="agent_limit",
        ),
        sa.CheckConstraint(
            "max_nonterminal_agentscope_runs IS NULL OR "
            "max_nonterminal_agentscope_runs BETWEEN 1 AND 1000000",
            name="agentscope_limit",
        ),
        sa.CheckConstraint(
            "max_nonterminal_codex_runs IS NULL OR "
            "max_nonterminal_codex_runs BETWEEN 1 AND 1000000",
            name="codex_limit",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^sha256:[a-f0-9]{64}$'", name="content_hash"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_quota_policy_version__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name="fk_quota_policy_version__created_by__app_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_quota_policy_version"),
        sa.UniqueConstraint(
            "tenant_id",
            "policy_id",
            "version_no",
            name="uq_quota_policy_version__tenant_policy_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "policy_id",
            "id",
            name="uq_quota_policy_version__tenant_policy_id",
        ),
    )
    op.create_index(
        "ix_quota_policy_version__tenant_policy_created",
        "quota_policy_version",
        ["tenant_id", "policy_id", "created_at", "id"],
    )
    op.create_foreign_key(
        "fk_quota_policy_version__tenant_policy",
        "quota_policy_version",
        "quota_policy",
        ["tenant_id", "policy_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_quota_policy__current_version",
        "quota_policy",
        "quota_policy_version",
        ["tenant_id", "id", "current_version_id"],
        ["tenant_id", "policy_id", "id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_tenant__quota_policy_id__quota_policy",
        "tenant",
        "quota_policy",
        ["quota_policy_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    for table_name in ("quota_policy", "quota_policy_version"):
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
            "CREATE TRIGGER trg_quota_policy_version__immutable "
            "BEFORE UPDATE OR DELETE ON quota_policy_version FOR EACH ROW "
            "EXECUTE FUNCTION guard_quota_policy_version_immutable()"
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
                f"SELECT tenant_id, id, 'quota_policy', '{action}' FROM role "
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
            AND resource_type = 'quota_policy';
        END LOOP; END $$"""))
    op.execute(
        sa.text(
            "DROP TRIGGER trg_quota_policy_version__immutable "
            "ON quota_policy_version"
        )
    )
    op.execute(sa.text("DROP FUNCTION guard_quota_policy_version_immutable()"))
    op.drop_constraint(
        "fk_tenant__quota_policy_id__quota_policy", "tenant", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_quota_policy__current_version", "quota_policy", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_quota_policy_version__tenant_policy",
        "quota_policy_version",
        type_="foreignkey",
    )
    for table_name in ("quota_policy_version", "quota_policy"):
        op.execute(sa.text(f"DROP POLICY tenant_isolation ON {table_name}"))
        op.execute(sa.text(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY"))
    op.drop_index(
        "ix_quota_policy_version__tenant_policy_created",
        table_name="quota_policy_version",
    )
    op.drop_table("quota_policy_version")
    op.drop_index("ix_quota_policy__tenant_status", table_name="quota_policy")
    op.drop_table("quota_policy")


_VERSION_IMMUTABILITY_GUARD = """
CREATE FUNCTION guard_quota_policy_version_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'quota policy versions are immutable';
END;
$$;
"""

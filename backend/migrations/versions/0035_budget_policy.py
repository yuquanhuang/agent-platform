"""Add durable periodic tenant model token budget policies.

Revision ID: 0035_budget_policy
Revises: 0034_quota_policy
Create Date: 2026-08-12
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0035_budget_policy"
down_revision: str | None = "0034_quota_policy"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY = (
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)
PERMISSIONS = ("create", "read", "list", "update", "disable")


def upgrade() -> None:
    op.add_column(
        "tenant",
        sa.Column("budget_policy_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_table(
        "budget_policy",
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
            name="fk_budget_policy__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name="fk_budget_policy__created_by__app_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_budget_policy"),
        sa.UniqueConstraint("tenant_id", name="uq_budget_policy__tenant_id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_budget_policy__tenant_id_id"),
    )
    op.create_index(
        "ix_budget_policy__tenant_status", "budget_policy", ["tenant_id", "status"]
    )
    op.create_table(
        "budget_policy_version",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("policy_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_no", sa.BigInteger(), nullable=False),
        sa.Column("period", sa.String(length=20), nullable=False),
        sa.Column("enforcement", sa.String(length=20), nullable=False),
        sa.Column("token_limit", sa.BigInteger(), nullable=False),
        sa.Column("cost_limit_amount", sa.Numeric(28, 8), nullable=True),
        sa.Column("cost_limit_currency", sa.String(length=3), nullable=True),
        sa.Column("price_catalog_version", sa.String(length=128), nullable=True),
        sa.Column("content_hash", sa.String(length=80), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version_no >= 1", name="version_no"),
        sa.CheckConstraint("period IN ('DAILY', 'MONTHLY')", name="period"),
        sa.CheckConstraint("enforcement = 'HARD'", name="enforcement"),
        sa.CheckConstraint("token_limit >= 1", name="token_limit"),
        sa.CheckConstraint(
            "(cost_limit_amount IS NULL) = (cost_limit_currency IS NULL)",
            name="cost_pair",
        ),
        sa.CheckConstraint(
            "cost_limit_amount IS NULL OR cost_limit_amount >= 0", name="cost_amount"
        ),
        sa.CheckConstraint(
            "cost_limit_currency IS NULL OR cost_limit_currency ~ '^[A-Z]{3}$'",
            name="cost_currency",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^sha256:[a-f0-9]{64}$'", name="content_hash"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_budget_policy_version__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name="fk_budget_policy_version__created_by__app_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_budget_policy_version"),
        sa.UniqueConstraint(
            "tenant_id",
            "policy_id",
            "version_no",
            name="uq_budget_policy_version__tenant_policy_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "policy_id",
            "id",
            name="uq_budget_policy_version__tenant_policy_id",
        ),
    )
    op.create_index(
        "ix_budget_policy_version__tenant_policy_created",
        "budget_policy_version",
        ["tenant_id", "policy_id", "created_at", "id"],
    )
    op.create_foreign_key(
        "fk_budget_policy_version__tenant_policy",
        "budget_policy_version",
        "budget_policy",
        ["tenant_id", "policy_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_budget_policy__current_version",
        "budget_policy",
        "budget_policy_version",
        ["tenant_id", "id", "current_version_id"],
        ["tenant_id", "policy_id", "id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_tenant__budget_policy_id__budget_policy",
        "tenant",
        "budget_policy",
        ["budget_policy_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column(
        "budget_reservation",
        sa.Column("budget_policy_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "budget_reservation",
        sa.Column(
            "budget_policy_version_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.add_column(
        "budget_reservation",
        sa.Column(
            "budget_period_started_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "budget_reservation",
        sa.Column("budget_period_ends_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_budget_reservation__budget_policy_version",
        "budget_reservation",
        "budget_policy_version",
        ["tenant_id", "budget_policy_id", "budget_policy_version_id"],
        ["tenant_id", "policy_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_budget_reservation__policy_period_snapshot",
        "budget_reservation",
        "(budget_policy_id IS NULL AND budget_policy_version_id IS NULL "
        "AND budget_period_started_at IS NULL AND budget_period_ends_at IS NULL) "
        "OR (budget_policy_id IS NOT NULL "
        "AND budget_policy_version_id IS NOT NULL "
        "AND budget_period_started_at IS NOT NULL "
        "AND budget_period_ends_at IS NOT NULL "
        "AND budget_period_started_at < budget_period_ends_at)",
    )
    op.create_index(
        "ix_budget_reservation__tenant_policy_period_status",
        "budget_reservation",
        [
            "tenant_id",
            "budget_policy_id",
            "budget_period_started_at",
            "status",
            "expires_at",
        ],
    )
    for table_name in ("budget_policy", "budget_policy_version"):
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
            "CREATE TRIGGER trg_budget_policy_version__immutable "
            "BEFORE UPDATE OR DELETE ON budget_policy_version FOR EACH ROW "
            "EXECUTE FUNCTION guard_budget_policy_version_immutable()"
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
                f"SELECT tenant_id, id, 'budget_policy', '{action}' FROM role "
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
            AND resource_type = 'budget_policy';
        END LOOP; END $$"""))
    op.execute(
        sa.text(
            "DROP TRIGGER trg_budget_policy_version__immutable "
            "ON budget_policy_version"
        )
    )
    op.execute(sa.text("DROP FUNCTION guard_budget_policy_version_immutable()"))
    for table_name in ("budget_policy_version", "budget_policy"):
        op.execute(sa.text(f"DROP POLICY tenant_isolation ON {table_name}"))
        op.execute(sa.text(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY"))
    op.drop_index(
        "ix_budget_reservation__tenant_policy_period_status",
        table_name="budget_reservation",
    )
    op.drop_constraint(
        "fk_budget_reservation__budget_policy_version",
        "budget_reservation",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_budget_reservation__policy_period_snapshot",
        "budget_reservation",
        type_="check",
    )
    for column_name in (
        "budget_period_ends_at",
        "budget_period_started_at",
        "budget_policy_version_id",
        "budget_policy_id",
    ):
        op.drop_column("budget_reservation", column_name)
    op.drop_constraint(
        "fk_tenant__budget_policy_id__budget_policy", "tenant", type_="foreignkey"
    )
    op.drop_column("tenant", "budget_policy_id")
    op.drop_constraint(
        "fk_budget_policy__current_version", "budget_policy", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_budget_policy_version__tenant_policy",
        "budget_policy_version",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_budget_policy_version__tenant_policy_created",
        table_name="budget_policy_version",
    )
    op.drop_table("budget_policy_version")
    op.drop_index("ix_budget_policy__tenant_status", table_name="budget_policy")
    op.drop_table("budget_policy")


_VERSION_IMMUTABILITY_GUARD = """
CREATE FUNCTION guard_budget_policy_version_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'budget policy versions are immutable';
END;
$$;
"""

"""Add immutable cost attribution provenance to model usage facts.

Revision ID: 0036_model_usage_cost_provenance
Revises: 0035_budget_policy
Create Date: 2026-08-12
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0036_model_usage_cost_provenance"
down_revision: str | None = "0035_budget_policy"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "price_catalog_version",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_ref", sa.String(length=2048), nullable=False),
        sa.Column("content_hash", sa.String(length=80), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency"),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="effective_range",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^sha256:[a-f0-9]{64}$'", name="content_hash"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_price_catalog_version__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name="fk_price_catalog_version__created_by__app_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_price_catalog_version"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_price_catalog_version__tenant_id_id"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "model",
            "effective_from",
            name="uq_price_catalog_version__tenant_provider_model_effective",
        ),
    )
    op.create_index(
        "ix_price_catalog_version__tenant_provider_model_effective",
        "price_catalog_version",
        ["tenant_id", "provider", "model", "effective_from"],
    )
    op.create_table(
        "price_catalog_rate",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("catalog_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dimension", sa.String(length=32), nullable=False),
        sa.Column("unit_tokens", sa.BigInteger(), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=28, scale=12), nullable=False),
        sa.CheckConstraint(
            "dimension IN ('input_tokens', 'output_tokens', 'reasoning_tokens', "
            "'cache_read_tokens', 'cache_write_tokens')",
            name="dimension",
        ),
        sa.CheckConstraint("unit_tokens >= 1", name="unit_tokens"),
        sa.CheckConstraint("unit_price >= 0", name="unit_price"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "catalog_version_id"],
            ["price_catalog_version.tenant_id", "price_catalog_version.id"],
            name="fk_price_catalog_rate__tenant_catalog_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_price_catalog_rate"),
        sa.UniqueConstraint(
            "tenant_id",
            "catalog_version_id",
            "dimension",
            name="uq_price_catalog_rate__tenant_version_dimension",
        ),
    )
    op.create_index(
        "ix_price_catalog_rate__tenant_catalog_version",
        "price_catalog_rate",
        ["tenant_id", "catalog_version_id"],
    )
    op.add_column(
        "model_usage", sa.Column("cost_source", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "model_usage",
        sa.Column(
            "price_catalog_version_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "model_usage",
        sa.Column("cost_details_json", postgresql.JSONB(), nullable=True),
    )
    op.create_check_constraint(
        "ck_model_usage__cost_source_pair",
        "model_usage",
        "(cost_amount IS NULL) = (cost_source IS NULL)",
    )
    op.create_foreign_key(
        "fk_model_usage__tenant_price_catalog_version",
        "model_usage",
        "price_catalog_version",
        ["tenant_id", "price_catalog_version_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    for table_name in ("price_catalog_version", "price_catalog_rate"):
        op.execute(sa.text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"CREATE POLICY tenant_isolation ON {table_name} "
                f"USING ({TENANT_POLICY}) WITH CHECK ({TENANT_POLICY})"
            )
        )
    op.create_check_constraint(
        "ck_model_usage__cost_source",
        "model_usage",
        "cost_source IS NULL OR cost_source IN "
        "('PROVIDER_REPORTED', 'CATALOG_CALCULATED')",
    )
    op.create_check_constraint(
        "ck_model_usage__catalog_provenance",
        "model_usage",
        "(cost_source = 'CATALOG_CALCULATED') = "
        "(price_catalog_version_id IS NOT NULL)",
    )
    op.execute(sa.text(_MODEL_USAGE_GUARD_FUNCTION))
    op.execute(sa.text(_PRICE_CATALOG_GUARD_FUNCTION))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_model_usage__immutable "
            "BEFORE UPDATE OR DELETE ON model_usage FOR EACH ROW "
            "EXECUTE FUNCTION guard_model_usage_immutable()"
        )
    )
    for table_name in ("price_catalog_version", "price_catalog_rate"):
        op.execute(
            sa.text(
                f"CREATE TRIGGER trg_{table_name}__immutable "
                f"BEFORE UPDATE OR DELETE ON {table_name} FOR EACH ROW "
                "EXECUTE FUNCTION guard_price_catalog_immutable()"
            )
        )


def downgrade() -> None:
    for table_name in ("price_catalog_rate", "price_catalog_version"):
        op.execute(sa.text(f"DROP TRIGGER trg_{table_name}__immutable ON {table_name}"))
    op.execute(sa.text("DROP FUNCTION guard_price_catalog_immutable()"))
    op.execute(sa.text("DROP TRIGGER trg_model_usage__immutable ON model_usage"))
    op.execute(sa.text("DROP FUNCTION guard_model_usage_immutable()"))
    op.drop_constraint(
        "ck_model_usage__catalog_provenance", "model_usage", type_="check"
    )
    op.drop_constraint(
        "fk_model_usage__tenant_price_catalog_version",
        "model_usage",
        type_="foreignkey",
    )
    op.drop_constraint("ck_model_usage__cost_source", "model_usage", type_="check")
    op.drop_constraint("ck_model_usage__cost_source_pair", "model_usage", type_="check")
    op.drop_column("model_usage", "cost_details_json")
    op.drop_column("model_usage", "price_catalog_version_id")
    op.drop_column("model_usage", "cost_source")
    for table_name in ("price_catalog_rate", "price_catalog_version"):
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table_name}"))
        op.execute(sa.text(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY"))
    op.drop_index(
        "ix_price_catalog_rate__tenant_catalog_version",
        table_name="price_catalog_rate",
    )
    op.drop_table("price_catalog_rate")
    op.drop_index(
        "ix_price_catalog_version__tenant_provider_model_effective",
        table_name="price_catalog_version",
    )
    op.drop_table("price_catalog_version")


_MODEL_USAGE_GUARD_FUNCTION = """
CREATE FUNCTION guard_model_usage_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'model usage facts are immutable';
END;
$$;
"""

TENANT_POLICY = (
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)

_PRICE_CATALOG_GUARD_FUNCTION = """
CREATE FUNCTION guard_price_catalog_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'price catalog versions and rates are immutable';
END;
$$;
"""

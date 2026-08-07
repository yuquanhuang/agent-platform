"""Add normalized tenant-scoped model usage facts.

Revision ID: 0007_model_gateway_usage
Revises: 0006_model_permissions
Create Date: 2026-08-07
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_model_gateway_usage"
down_revision: str | None = "0006_model_permissions"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY_EXPRESSION = (
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        "model_usage",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("provider_request_id", sa.String(length=512), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("reasoning_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_write_tokens", sa.BigInteger(), nullable=False),
        sa.Column("token_estimated", sa.Boolean(), nullable=False),
        sa.Column("cost_amount", sa.Numeric(precision=28, scale=8), nullable=True),
        sa.Column("cost_currency", sa.String(length=3), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("input_tokens >= 0", name="ck_model_usage__input_tokens"),
        sa.CheckConstraint("output_tokens >= 0", name="ck_model_usage__output_tokens"),
        sa.CheckConstraint(
            "reasoning_tokens >= 0", name="ck_model_usage__reasoning_tokens"
        ),
        sa.CheckConstraint(
            "cache_read_tokens >= 0", name="ck_model_usage__cache_read_tokens"
        ),
        sa.CheckConstraint(
            "cache_write_tokens >= 0", name="ck_model_usage__cache_write_tokens"
        ),
        sa.CheckConstraint(
            "cost_amount IS NULL OR cost_amount >= 0",
            name="ck_model_usage__cost_amount",
        ),
        sa.CheckConstraint(
            "(cost_amount IS NULL) = (cost_currency IS NULL)",
            name="ck_model_usage__cost_pair",
        ),
        sa.CheckConstraint(
            "finished_at >= started_at", name="ck_model_usage__time_order"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_model_usage__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_model_usage"),
    )
    op.create_index(
        "ix_model_usage__tenant_id_run_id",
        "model_usage",
        ["tenant_id", "run_id"],
        unique=False,
    )
    op.create_index(
        "ix_model_usage__tenant_id_finished_at",
        "model_usage",
        ["tenant_id", "finished_at"],
        unique=False,
    )
    op.execute(sa.text("ALTER TABLE model_usage ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE model_usage FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON model_usage "
            f"USING ({TENANT_POLICY_EXPRESSION}) "
            f"WITH CHECK ({TENANT_POLICY_EXPRESSION})"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON model_usage"))
    op.execute(sa.text("ALTER TABLE model_usage DISABLE ROW LEVEL SECURITY"))
    op.drop_index("ix_model_usage__tenant_id_finished_at", table_name="model_usage")
    op.drop_index("ix_model_usage__tenant_id_run_id", table_name="model_usage")
    op.drop_table("model_usage")

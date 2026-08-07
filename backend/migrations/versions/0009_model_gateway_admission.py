"""Add conservative Model Gateway token reservations and RPM windows.

Revision ID: 0009_model_gateway_admission
Revises: 0008_model_binding_snapshot
Create Date: 2026-08-07
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_model_gateway_admission"
down_revision: str | None = "0008_model_binding_snapshot"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY_EXPRESSION = (
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        "budget_reservation",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("model_binding_id", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("reserved_tokens", sa.BigInteger(), nullable=False),
        sa.Column("consumed_tokens", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('RESERVED', 'CONSUMED', 'RELEASED')", name="status"
        ),
        sa.CheckConstraint("reserved_tokens >= 1", name="reserved_tokens"),
        sa.CheckConstraint(
            "consumed_tokens IS NULL OR consumed_tokens >= 0",
            name="consumed_tokens",
        ),
        sa.CheckConstraint(
            "(status = 'CONSUMED') = (consumed_tokens IS NOT NULL)",
            name="consumed_status",
        ),
        sa.CheckConstraint(
            "(status = 'RESERVED') = (finished_at IS NULL)",
            name="finished_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_budget_reservation__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_budget_reservation"),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "idempotency_key",
            name="uq_budget_reservation__tenant_run_idempotency",
        ),
    )
    op.create_index(
        "ix_budget_reservation__tenant_run_status_expires",
        "budget_reservation",
        ["tenant_id", "run_id", "status", "expires_at"],
        unique=False,
    )

    op.create_table(
        "model_rate_limit_window",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model_binding_id", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("request_count >= 1", name="request_count"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_model_rate_limit_window__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "model_binding_id",
            "provider",
            "window_started_at",
            name="pk_model_rate_limit_window",
        ),
    )
    op.create_index(
        "ix_model_rate_limit_window__window_started_at",
        "model_rate_limit_window",
        ["window_started_at"],
        unique=False,
    )

    for table_name in ("budget_reservation", "model_rate_limit_window"):
        op.execute(sa.text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"CREATE POLICY tenant_isolation ON {table_name} "
                f"USING ({TENANT_POLICY_EXPRESSION}) "
                f"WITH CHECK ({TENANT_POLICY_EXPRESSION})"
            )
        )


def downgrade() -> None:
    for table_name in ("model_rate_limit_window", "budget_reservation"):
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table_name}"))
        op.execute(sa.text(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY"))
    op.drop_index(
        "ix_model_rate_limit_window__window_started_at",
        table_name="model_rate_limit_window",
    )
    op.drop_table("model_rate_limit_window")
    op.drop_index(
        "ix_budget_reservation__tenant_run_status_expires",
        table_name="budget_reservation",
    )
    op.drop_table("budget_reservation")

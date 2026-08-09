"""Persist Run Workflow starts and reconciliation timestamps.

Revision ID: 0019_run_workflow_reconciliation
Revises: 0018_run_control_permissions
Create Date: 2026-08-08
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0019_run_workflow_reconciliation"
down_revision: str | None = "0018_run_control_permissions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "agent_run", sa.Column("temporal_run_id", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "agent_run",
        sa.Column("workflow_start_outcome", sa.String(length=24), nullable=True),
    )
    op.add_column(
        "agent_run",
        sa.Column("workflow_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "agent_run",
        sa.Column("cancelling_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE agent_run SET cancelling_at = created_at "
            "WHERE status = 'CANCELLING' AND cancelling_at IS NULL"
        )
    )
    op.create_check_constraint(
        "ck_agent_run__cancelling_at",
        "agent_run",
        "cancelling_at IS NULL OR cancelling_at >= created_at",
    )
    op.create_check_constraint(
        "ck_agent_run__workflow_start_mapping",
        "agent_run",
        "(temporal_run_id IS NULL AND workflow_start_outcome IS NULL "
        "AND workflow_started_at IS NULL) OR "
        "(workflow_id IS NOT NULL AND temporal_run_id IS NOT NULL "
        "AND workflow_start_outcome IS NOT NULL AND workflow_started_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_agent_run__workflow_start_outcome",
        "agent_run",
        "workflow_start_outcome IS NULL OR workflow_start_outcome IN "
        "('STARTED','ALREADY_EXISTS')",
    )
    op.create_unique_constraint(
        "uq_agent_run__tenant_workflow_id",
        "agent_run",
        ["tenant_id", "workflow_id"],
    )
    op.create_index(
        "ix_agent_run__tenant_status_cancelling_at",
        "agent_run",
        ["tenant_id", "status", "cancelling_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_run__tenant_status_cancelling_at", table_name="agent_run")
    op.drop_constraint("uq_agent_run__tenant_workflow_id", "agent_run", type_="unique")
    op.drop_constraint(
        "ck_agent_run__workflow_start_outcome", "agent_run", type_="check"
    )
    op.drop_constraint(
        "ck_agent_run__workflow_start_mapping", "agent_run", type_="check"
    )
    op.drop_constraint("ck_agent_run__cancelling_at", "agent_run", type_="check")
    op.drop_column("agent_run", "cancelling_at")
    op.drop_column("agent_run", "workflow_started_at")
    op.drop_column("agent_run", "workflow_start_outcome")
    op.drop_column("agent_run", "temporal_run_id")

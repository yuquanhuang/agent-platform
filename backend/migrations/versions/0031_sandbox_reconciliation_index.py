"""Add the bounded expired Sandbox lease reconciliation index.

Revision ID: 0031_sandbox_reconcile_index
Revises: 0030_approval_signal_reconcile
Create Date: 2026-08-11
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0031_sandbox_reconcile_index"
down_revision: str | None = "0030_approval_signal_reconcile"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "ix_sandbox_instance__tenant_expired_lease",
        "sandbox_instance",
        ["tenant_id", "lease_expires_at"],
        postgresql_where=sa.text(
            "lease_expires_at IS NOT NULL AND status IN ('READY','IN_USE')"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sandbox_instance__tenant_expired_lease",
        table_name="sandbox_instance",
    )

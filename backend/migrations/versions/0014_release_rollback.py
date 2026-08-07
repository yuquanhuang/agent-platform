"""Add explicit Release source fields for historical Snapshot rollback.

Revision ID: 0014_release_rollback
Revises: 0013_deployment_activation
Create Date: 2026-08-07
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_release_rollback"
down_revision: str | None = "0013_deployment_activation"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "release",
        sa.Column(
            "release_kind",
            sa.String(length=20),
            server_default=sa.text("'PUBLISH'"),
            nullable=False,
        ),
    )
    op.add_column(
        "release",
        sa.Column(
            "requested_snapshot_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.alter_column(
        "release",
        "expected_agent_version",
        existing_type=sa.BigInteger(),
        nullable=True,
    )
    op.drop_constraint(
        "expected_agent_version",
        "release",
        type_="check",
    )
    op.create_check_constraint(
        "expected_agent_version",
        "release",
        "expected_agent_version IS NULL OR expected_agent_version >= 1",
    )
    op.create_check_constraint(
        "release_kind",
        "release",
        "release_kind IN ('PUBLISH', 'ROLLBACK')",
    )
    op.create_check_constraint(
        "release_source",
        "release",
        "(release_kind = 'PUBLISH' AND expected_agent_version IS NOT NULL "
        "AND requested_snapshot_id IS NULL) OR "
        "(release_kind = 'ROLLBACK' AND expected_agent_version IS NULL "
        "AND requested_snapshot_id IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_release__tenant_requested_snapshot__agent_snapshot",
        "release",
        "agent_snapshot",
        ["tenant_id", "requested_snapshot_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_release__tenant_requested_snapshot",
        "release",
        ["tenant_id", "requested_snapshot_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_constraint("release_source", "release", type_="check")
    op.execute(
        sa.text(
            "UPDATE release AS r SET expected_agent_version = a.resource_version "
            "FROM agent_definition AS a "
            "WHERE r.release_kind = 'ROLLBACK' "
            "AND a.tenant_id = r.tenant_id AND a.id = r.agent_id"
        )
    )
    op.drop_index("ix_release__tenant_requested_snapshot", table_name="release")
    op.drop_constraint(
        "fk_release__tenant_requested_snapshot__agent_snapshot",
        "release",
        type_="foreignkey",
    )
    op.drop_constraint("release_kind", "release", type_="check")
    op.drop_constraint(
        "expected_agent_version",
        "release",
        type_="check",
    )
    op.create_check_constraint(
        "expected_agent_version",
        "release",
        "expected_agent_version >= 1",
    )
    op.alter_column(
        "release",
        "expected_agent_version",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.drop_column("release", "requested_snapshot_id")
    op.drop_column("release", "release_kind")

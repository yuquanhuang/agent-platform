"""Add tenant-scoped transactional Outbox.

Revision ID: 0003_temporal_outbox
Revises: 0002_iam_rbac_foundation
Create Date: 2026-08-06
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_temporal_outbox"
down_revision: str | None = "0002_iam_rbac_foundation"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY_EXPRESSION = (
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        "outbox_event",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "payload_schema_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PUBLISHING', 'PUBLISHED', 'DEAD')",
            name="ck_outbox_event__status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_outbox_event__attempts"),
        sa.CheckConstraint(
            "payload_schema_version >= 1",
            name="ck_outbox_event__payload_schema_version",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_outbox_event__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_event"),
    )
    op.create_index(
        "ix_outbox_event__status_next_attempt_at",
        "outbox_event",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_outbox_event__tenant_id_id",
        "outbox_event",
        ["tenant_id", "id"],
        unique=False,
    )
    op.execute(sa.text("ALTER TABLE outbox_event ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE outbox_event FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON outbox_event "
            f"USING ({TENANT_POLICY_EXPRESSION}) "
            f"WITH CHECK ({TENANT_POLICY_EXPRESSION})"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON outbox_event"))
    op.execute(sa.text("ALTER TABLE outbox_event DISABLE ROW LEVEL SECURITY"))
    op.drop_index("ix_outbox_event__tenant_id_id", table_name="outbox_event")
    op.drop_index("ix_outbox_event__status_next_attempt_at", table_name="outbox_event")
    op.drop_table("outbox_event")

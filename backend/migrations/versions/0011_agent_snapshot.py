"""Add immutable Agent Versions and compiled Snapshots.

Revision ID: 0011_agent_snapshot
Revises: 0010_agent_draft
Create Date: 2026-08-07
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_agent_snapshot"
down_revision: str | None = "0010_agent_draft"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY_EXPRESSION = (
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        "agent_version",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_no", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_from_version_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("release_note", sa.String(length=2000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint("version_no >= 1", name="version_no"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_agent_version__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agent_definition.tenant_id", "agent_definition.id"],
            name="fk_agent_version__tenant_agent__agent_definition",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "created_from_version_id"],
            ["agent_version.tenant_id", "agent_version.id"],
            name="fk_agent_version__tenant_created_from__agent_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name="fk_agent_version__created_by__app_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_version"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_agent_version__tenant_id_id"),
        sa.UniqueConstraint(
            "agent_id", "version_no", name="uq_agent_version__agent_id_version_no"
        ),
    )
    op.create_index(
        "ix_agent_version__tenant_agent_version_no",
        "agent_version",
        ["tenant_id", "agent_id", "version_no"],
        unique=False,
    )

    op.create_table(
        "agent_snapshot",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("content_json", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(length=80), nullable=False),
        sa.Column("compiler_input_hash", sa.String(length=80), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint(
            "content_hash ~ '^sha256:[a-f0-9]{64}$'", name="content_hash"
        ),
        sa.CheckConstraint(
            "compiler_input_hash ~ '^sha256:[a-f0-9]{64}$'",
            name="compiler_input_hash",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(content_json) = 'object'", name="content_json"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_agent_snapshot__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_version_id"],
            ["agent_version.tenant_id", "agent_version.id"],
            name="fk_agent_snapshot__tenant_version__agent_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name="fk_agent_snapshot__created_by__app_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_snapshot"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_agent_snapshot__tenant_id_id"),
        sa.UniqueConstraint(
            "agent_version_id", name="uq_agent_snapshot__agent_version_id"
        ),
    )
    op.create_index(
        "ix_agent_snapshot__tenant_created_at",
        "agent_snapshot",
        ["tenant_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_agent_snapshot__content_json_gin",
        "agent_snapshot",
        ["content_json"],
        unique=False,
        postgresql_using="gin",
    )

    for table_name in ("agent_version", "agent_snapshot"):
        op.execute(sa.text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"CREATE POLICY tenant_isolation ON {table_name} "
                f"USING ({TENANT_POLICY_EXPRESSION}) "
                f"WITH CHECK ({TENANT_POLICY_EXPRESSION})"
            )
        )

    op.execute(sa.text("""CREATE FUNCTION reject_agent_release_fact_mutation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'published Agent Versions and Snapshots are immutable';
            END;
            $$"""))
    for table_name in ("agent_version", "agent_snapshot"):
        op.execute(
            sa.text(
                f"CREATE TRIGGER trg_{table_name}__immutable "
                f"BEFORE UPDATE OR DELETE ON {table_name} "
                "FOR EACH ROW EXECUTE FUNCTION reject_agent_release_fact_mutation()"
            )
        )


def downgrade() -> None:
    for table_name in ("agent_snapshot", "agent_version"):
        op.execute(
            sa.text(
                f"DROP TRIGGER IF EXISTS trg_{table_name}__immutable ON {table_name}"
            )
        )
    op.execute(sa.text("DROP FUNCTION IF EXISTS reject_agent_release_fact_mutation"))
    for table_name in ("agent_snapshot", "agent_version"):
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table_name}"))
        op.execute(sa.text(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY"))
    op.drop_index("ix_agent_snapshot__content_json_gin", table_name="agent_snapshot")
    op.drop_index("ix_agent_snapshot__tenant_created_at", table_name="agent_snapshot")
    op.drop_table("agent_snapshot")
    op.drop_index(
        "ix_agent_version__tenant_agent_version_no", table_name="agent_version"
    )
    op.drop_table("agent_version")

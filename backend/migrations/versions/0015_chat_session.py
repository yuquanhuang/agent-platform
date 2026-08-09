"""Add user-owned Sessions pinned to active Deployments.

Revision ID: 0015_chat_session
Revises: 0014_release_rollback
Create Date: 2026-08-08
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_chat_session"
down_revision: str | None = "0014_release_rollback"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY_EXPRESSION = (
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)
SESSION_PERMISSIONS = ("create", "read", "list", "update", "delete")


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_deployment__tenant_id_agent_id",
        "deployment",
        ["tenant_id", "id", "agent_id"],
    )
    op.create_table(
        "chat_session",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "default_deployment_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'ACTIVE'"),
            nullable=False,
        ),
        sa.Column("cursor_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("branch_root_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "metadata_schema_version",
            sa.String(length=32),
            server_default=sa.text("'session-metadata/v1'"),
            nullable=False,
        ),
        sa.Column(
            "resource_version",
            sa.BigInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
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
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'ARCHIVED', 'DELETED')", name="status"
        ),
        sa.CheckConstraint("resource_version >= 1", name="resource_version"),
        sa.CheckConstraint(
            "jsonb_typeof(metadata_json) = 'object'", name="metadata_json"
        ),
        sa.CheckConstraint(
            "(status = 'ACTIVE' AND archived_at IS NULL AND deleted_at IS NULL) OR "
            "(status = 'ARCHIVED' AND archived_at IS NOT NULL "
            "AND deleted_at IS NULL) OR "
            "(status = 'DELETED' AND archived_at IS NOT NULL "
            "AND deleted_at IS NOT NULL)",
            name="lifecycle_timestamps",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_chat_session__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["tenant_member.tenant_id", "tenant_member.user_id"],
            name="fk_chat_session__tenant_user__tenant_member",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agent_definition.tenant_id", "agent_definition.id"],
            name="fk_chat_session__tenant_agent__agent_definition",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "default_deployment_id", "agent_id"],
            ["deployment.tenant_id", "deployment.id", "deployment.agent_id"],
            name="fk_chat_session__tenant_deployment_agent__deployment",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chat_session"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_chat_session__tenant_id_id"),
    )
    op.create_index(
        "ix_chat_session__tenant_user_updated_at",
        "chat_session",
        ["tenant_id", "user_id", sa.text("updated_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_chat_session__tenant_agent_created_at",
        "chat_session",
        ["tenant_id", "agent_id", sa.text("created_at DESC")],
        unique=False,
    )
    op.execute(sa.text("ALTER TABLE chat_session ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE chat_session FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON chat_session "
            f"USING ({TENANT_POLICY_EXPRESSION}) "
            f"WITH CHECK ({TENANT_POLICY_EXPRESSION})"
        )
    )

    for action in SESSION_PERMISSIONS:
        op.execute(sa.text(f"""DO $$ DECLARE tenant_uuid uuid; BEGIN
                FOR tenant_uuid IN SELECT tenant_id FROM role
                WHERE built_in IS TRUE AND code = 'tenant_admin' LOOP
                    PERFORM set_config(
                        'app.current_tenant_id', tenant_uuid::text, true
                    );
                    INSERT INTO role_permission
                        (tenant_id, role_id, resource_type, action)
                    SELECT tenant_id, id, 'session', '{action}' FROM role
                    WHERE tenant_id = tenant_uuid AND built_in IS TRUE
                    AND code = 'tenant_admin' ON CONFLICT DO NOTHING;
                END LOOP; END $$"""))


def downgrade() -> None:
    for action in SESSION_PERMISSIONS:
        op.execute(sa.text(f"""DO $$ DECLARE tenant_uuid uuid; BEGIN
                FOR tenant_uuid IN SELECT tenant_id FROM role
                WHERE built_in IS TRUE AND code = 'tenant_admin' LOOP
                    PERFORM set_config(
                        'app.current_tenant_id', tenant_uuid::text, true
                    );
                    DELETE FROM role_permission WHERE tenant_id = tenant_uuid
                    AND resource_type = 'session' AND action = '{action}';
                END LOOP; END $$"""))
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON chat_session"))
    op.execute(sa.text("ALTER TABLE chat_session DISABLE ROW LEVEL SECURITY"))
    op.drop_index("ix_chat_session__tenant_agent_created_at", table_name="chat_session")
    op.drop_index("ix_chat_session__tenant_user_updated_at", table_name="chat_session")
    op.drop_table("chat_session")
    op.drop_constraint(
        "uq_deployment__tenant_id_agent_id", "deployment", type_="unique"
    )

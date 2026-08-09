"""Add immutable Session message chains and message permissions.

Revision ID: 0016_chat_message
Revises: 0015_chat_session
Create Date: 2026-08-08
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_chat_message"
down_revision: str | None = "0015_chat_session"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY_EXPRESSION = (
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)
MESSAGE_PERMISSIONS = ("read", "list")


def upgrade() -> None:
    op.create_table(
        "chat_message",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("parent_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content_parts_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "content_schema_version",
            sa.String(length=32),
            server_default=sa.text("'message-content/v1'"),
            nullable=False,
        ),
        sa.Column("source_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint(
            "role IN ('USER', 'ASSISTANT', 'SYSTEM', 'TOOL')", name="role"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(content_parts_json) = 'array' "
            "AND jsonb_array_length(content_parts_json) >= 1",
            name="content_parts_json",
        ),
        sa.CheckConstraint(
            "parent_message_id IS NULL OR parent_message_id <> id",
            name="parent_not_self",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_chat_message__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["chat_session.tenant_id", "chat_session.id"],
            name="fk_chat_message__tenant_session__chat_session",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "parent_message_id", "session_id"],
            ["chat_message.tenant_id", "chat_message.id", "chat_message.session_id"],
            name="fk_chat_message__tenant_parent_session__chat_message",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name="fk_chat_message__created_by__app_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chat_message"),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            "session_id",
            name="uq_chat_message__tenant_id_session_id",
        ),
    )
    op.create_index(
        "ix_chat_message__tenant_session_branch_created_at",
        "chat_message",
        ["tenant_id", "session_id", "branch_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_chat_message__tenant_session_parent",
        "chat_message",
        ["tenant_id", "session_id", "parent_message_id"],
        unique=False,
    )
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_chat_message__branch_parent ON chat_message "
            "(tenant_id, session_id, branch_id, parent_message_id) "
            "NULLS NOT DISTINCT"
        )
    )
    op.execute(sa.text("ALTER TABLE chat_message ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE chat_message FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON chat_message "
            f"USING ({TENANT_POLICY_EXPRESSION}) "
            f"WITH CHECK ({TENANT_POLICY_EXPRESSION})"
        )
    )
    op.execute(
        sa.text(
            "CREATE FUNCTION reject_chat_message_mutation() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "RAISE EXCEPTION 'chat_message facts are immutable'; END; $$"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_chat_message__immutable "
            "BEFORE UPDATE OR DELETE ON chat_message FOR EACH ROW "
            "EXECUTE FUNCTION reject_chat_message_mutation()"
        )
    )
    op.create_foreign_key(
        "fk_chat_session__tenant_cursor_session__chat_message",
        "chat_session",
        "chat_message",
        ["tenant_id", "cursor_message_id", "id"],
        ["tenant_id", "id", "session_id"],
        ondelete="RESTRICT",
    )

    for action in MESSAGE_PERMISSIONS:
        op.execute(sa.text(f"""DO $$ DECLARE tenant_uuid uuid; BEGIN
                FOR tenant_uuid IN SELECT tenant_id FROM role
                WHERE built_in IS TRUE AND code = 'tenant_admin' LOOP
                    PERFORM set_config(
                        'app.current_tenant_id', tenant_uuid::text, true
                    );
                    INSERT INTO role_permission
                        (tenant_id, role_id, resource_type, action)
                    SELECT tenant_id, id, 'message', '{action}' FROM role
                    WHERE tenant_id = tenant_uuid AND built_in IS TRUE
                    AND code = 'tenant_admin' ON CONFLICT DO NOTHING;
                END LOOP; END $$"""))


def downgrade() -> None:
    for action in MESSAGE_PERMISSIONS:
        op.execute(sa.text(f"""DO $$ DECLARE tenant_uuid uuid; BEGIN
                FOR tenant_uuid IN SELECT tenant_id FROM role
                WHERE built_in IS TRUE AND code = 'tenant_admin' LOOP
                    PERFORM set_config(
                        'app.current_tenant_id', tenant_uuid::text, true
                    );
                    DELETE FROM role_permission WHERE tenant_id = tenant_uuid
                    AND resource_type = 'message' AND action = '{action}';
                END LOOP; END $$"""))
    op.drop_constraint(
        "fk_chat_session__tenant_cursor_session__chat_message",
        "chat_session",
        type_="foreignkey",
    )
    op.execute(sa.text("UPDATE chat_session SET cursor_message_id = NULL"))
    op.execute(sa.text("DROP TRIGGER trg_chat_message__immutable ON chat_message"))
    op.execute(sa.text("DROP FUNCTION reject_chat_message_mutation()"))
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON chat_message"))
    op.execute(sa.text("ALTER TABLE chat_message DISABLE ROW LEVEL SECURITY"))
    op.drop_index("uq_chat_message__branch_parent", table_name="chat_message")
    op.drop_index("ix_chat_message__tenant_session_parent", table_name="chat_message")
    op.drop_index(
        "ix_chat_message__tenant_session_branch_created_at",
        table_name="chat_message",
    )
    op.drop_table("chat_message")

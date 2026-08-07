"""Add Agent Draft definitions, bindings and tenant-admin permissions.

Revision ID: 0010_agent_draft
Revises: 0009_model_gateway_admission
Create Date: 2026-08-07
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_agent_draft"
down_revision: str | None = "0009_model_gateway_admission"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY_EXPRESSION = (
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)
AGENT_PERMISSIONS = (
    "create",
    "read",
    "list",
    "update",
    "delete",
    "disable",
    "publish",
)


def upgrade() -> None:
    op.create_table(
        "agent_definition",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", postgresql.CITEXT(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column("runtime_type", sa.String(length=20), nullable=False),
        sa.Column(
            "visibility",
            sa.String(length=20),
            server_default=sa.text("'private'"),
            nullable=False,
        ),
        sa.Column(
            "tags_json",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "default_language",
            sa.String(length=16),
            server_default=sa.text("'zh-CN'"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'DRAFT'"),
            nullable=False,
        ),
        sa.Column("active_deployment_id", postgresql.UUID(as_uuid=True), nullable=True),
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
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "runtime_type IN ('agentscope', 'codex')", name="runtime_type"
        ),
        sa.CheckConstraint("visibility IN ('private', 'tenant')", name="visibility"),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'ACTIVE', 'DISABLED', 'DELETING', 'DELETED')",
            name="status",
        ),
        sa.CheckConstraint("resource_version >= 1", name="resource_version"),
        sa.CheckConstraint("jsonb_typeof(tags_json) = 'array'", name="tags_json"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_agent_definition__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["app_user.id"],
            name="fk_agent_definition__owner_user_id__app_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name="fk_agent_definition__created_by__app_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["app_user.id"],
            name="fk_agent_definition__updated_by__app_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["deleted_by"],
            ["app_user.id"],
            name="fk_agent_definition__deleted_by__app_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_definition"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_agent_definition__tenant_id_id"
        ),
    )
    op.create_index(
        "uq_agent_definition__tenant_code_active",
        "agent_definition",
        ["tenant_id", "code"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_agent_definition__tenant_status_created_at",
        "agent_definition",
        ["tenant_id", "status", "created_at"],
        unique=False,
    )

    op.create_table(
        "agent_binding",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_policy", sa.String(length=24), nullable=False),
        sa.Column("fixed_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("binding_role", sa.String(length=32), nullable=True),
        sa.Column(
            "configuration_json",
            postgresql.JSONB(none_as_null=True),
            nullable=True,
        ),
        sa.Column("configuration_schema_version", sa.String(length=16), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint(
            "resource_type IN ('prompt', 'skill', 'mcp', 'model', 'knowledge', "
            "'sandbox', 'agent')",
            name="resource_type",
        ),
        sa.CheckConstraint(
            "version_policy IN ('fixed', 'resolve_on_publish')",
            name="version_policy",
        ),
        sa.CheckConstraint(
            "(version_policy = 'fixed' AND fixed_version_id IS NOT NULL) OR "
            "(version_policy = 'resolve_on_publish' AND fixed_version_id IS NULL)",
            name="version_policy_version",
        ),
        sa.CheckConstraint(
            "binding_role IS NULL OR binding_role IN "
            "('primary', 'fallback_1', 'fallback_2')",
            name="binding_role",
        ),
        sa.CheckConstraint(
            "resource_type = 'model' OR "
            "(binding_role IS NULL AND configuration_json IS NULL AND "
            "configuration_schema_version IS NULL)",
            name="model_routing_fields",
        ),
        sa.CheckConstraint(
            "configuration_json IS NULL OR "
            "(binding_role = 'primary' AND "
            "configuration_schema_version = 'model-routing/v1' AND "
            "jsonb_typeof(configuration_json) = 'object')",
            name="routing_configuration",
        ),
        sa.CheckConstraint(
            "binding_role = 'primary' OR "
            "(configuration_json IS NULL AND configuration_schema_version IS NULL)",
            name="fallback_configuration",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_agent_binding__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agent_definition.tenant_id", "agent_definition.id"],
            name="fk_agent_binding__tenant_agent__agent_definition",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name="fk_agent_binding__created_by__app_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["app_user.id"],
            name="fk_agent_binding__updated_by__app_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_binding"),
    )
    op.create_index(
        "uq_agent_binding__agent_resource_role",
        "agent_binding",
        [
            "agent_id",
            "resource_type",
            "resource_id",
            sa.text("coalesce(binding_role, '')"),
        ],
        unique=True,
    )
    op.create_index(
        "uq_agent_binding__agent_model_role",
        "agent_binding",
        ["agent_id", "binding_role"],
        unique=True,
        postgresql_where=sa.text(
            "resource_type = 'model' AND binding_role IS NOT NULL"
        ),
    )
    op.create_index(
        "ix_agent_binding__tenant_agent",
        "agent_binding",
        ["tenant_id", "agent_id"],
        unique=False,
    )
    op.create_index(
        "ix_agent_binding__tenant_resource",
        "agent_binding",
        ["tenant_id", "resource_type", "resource_id"],
        unique=False,
    )

    for table_name in ("agent_definition", "agent_binding"):
        op.execute(sa.text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"CREATE POLICY tenant_isolation ON {table_name} "
                f"USING ({TENANT_POLICY_EXPRESSION}) "
                f"WITH CHECK ({TENANT_POLICY_EXPRESSION})"
            )
        )

    for action in AGENT_PERMISSIONS:
        op.execute(sa.text(f"""DO $$ DECLARE tenant_uuid uuid; BEGIN
                FOR tenant_uuid IN SELECT tenant_id FROM role
                WHERE built_in IS TRUE AND code = 'tenant_admin' LOOP
                    PERFORM set_config(
                        'app.current_tenant_id', tenant_uuid::text, true
                    );
                    INSERT INTO role_permission
                        (tenant_id, role_id, resource_type, action)
                    SELECT tenant_id, id, 'agent', '{action}' FROM role
                    WHERE tenant_id = tenant_uuid AND built_in IS TRUE
                    AND code = 'tenant_admin' ON CONFLICT DO NOTHING;
                END LOOP; END $$"""))


def downgrade() -> None:
    for action in AGENT_PERMISSIONS:
        op.execute(sa.text(f"""DO $$ DECLARE tenant_uuid uuid; BEGIN
                FOR tenant_uuid IN SELECT tenant_id FROM role
                WHERE built_in IS TRUE AND code = 'tenant_admin' LOOP
                    PERFORM set_config(
                        'app.current_tenant_id', tenant_uuid::text, true
                    );
                    DELETE FROM role_permission WHERE tenant_id = tenant_uuid
                    AND resource_type = 'agent' AND action = '{action}';
                END LOOP; END $$"""))
    for table_name in ("agent_binding", "agent_definition"):
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table_name}"))
        op.execute(sa.text(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY"))
    op.drop_index("ix_agent_binding__tenant_resource", table_name="agent_binding")
    op.drop_index("ix_agent_binding__tenant_agent", table_name="agent_binding")
    op.drop_index("uq_agent_binding__agent_model_role", table_name="agent_binding")
    op.drop_index("uq_agent_binding__agent_resource_role", table_name="agent_binding")
    op.drop_table("agent_binding")
    op.drop_index(
        "ix_agent_definition__tenant_status_created_at",
        table_name="agent_definition",
    )
    op.drop_index(
        "uq_agent_definition__tenant_code_active", table_name="agent_definition"
    )
    op.drop_table("agent_definition")

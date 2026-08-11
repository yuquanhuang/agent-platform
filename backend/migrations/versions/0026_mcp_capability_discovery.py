"""Add immutable MCP capability discovery evidence and permissions.

Revision ID: 0026_mcp_capability_discovery
Revises: 0025_skill_supply_chain_scan
Create Date: 2026-08-10
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0026_mcp_capability_discovery"
down_revision: str | None = "0025_skill_supply_chain_scan"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY_EXPRESSION = (
    "tenant_id = nullif(current_setting('app.current_tenant_id', true), '')::uuid"
)
MCP_PERMISSIONS = (
    "create",
    "read",
    "list",
    "update",
    "delete",
    "publish",
    "rollback",
    "disable",
    "execute",
)


def upgrade() -> None:
    op.create_table(
        "mcp_capability_discovery",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("definition_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("draft_resource_version", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("protocol_version", sa.String(length=32), nullable=True),
        sa.Column("server_name", sa.String(length=128), nullable=True),
        sa.Column("server_version", sa.String(length=64), nullable=True),
        sa.Column("tools_json", postgresql.JSONB(), nullable=False),
        sa.Column("capability_hash", sa.String(length=80), nullable=True),
        sa.Column(
            "findings_json",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("discovered_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_discovery_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("published_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "draft_resource_version >= 1", name="draft_resource_version"
        ),
        sa.CheckConstraint("status IN ('PASSED','REJECTED','FAILED')", name="status"),
        sa.CheckConstraint(
            "content_hash ~ '^sha256:[a-f0-9]{64}$'", name="content_hash"
        ),
        sa.CheckConstraint(
            "capability_hash IS NULL OR capability_hash ~ '^sha256:[a-f0-9]{64}$'",
            name="capability_hash",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(findings_json) = 'array'", name="findings_json"
        ),
        sa.CheckConstraint("jsonb_typeof(tools_json) = 'array'", name="tools_json"),
        sa.CheckConstraint(
            "published_version_id IS NULL OR status = 'PASSED'",
            name="published_version_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_mcp_capability_discovery__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "definition_id"],
            ["resource_definition.tenant_id", "resource_definition.id"],
            name="fk_mcp_capability_discovery__tenant_definition",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operation_record.id"],
            name="fk_mcp_capability_discovery__operation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["discovered_by"],
            ["app_user.id"],
            name="fk_mcp_capability_discovery__discovered_by__app_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "published_version_id"],
            ["resource_version.tenant_id", "resource_version.id"],
            name="fk_mcp_capability_discovery__tenant_published_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "source_discovery_id"],
            [
                "mcp_capability_discovery.tenant_id",
                "mcp_capability_discovery.id",
            ],
            name="fk_mcp_capability_discovery__tenant_source",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mcp_capability_discovery"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_mcp_capability_discovery__tenant_id_id"
        ),
        sa.UniqueConstraint(
            "operation_id", name="uq_mcp_capability_discovery__operation_id"
        ),
        sa.UniqueConstraint(
            "published_version_id",
            name="uq_mcp_capability_discovery__published_version_id",
        ),
    )
    op.create_index(
        "ix_mcp_capability_discovery__tenant_definition_discovered_at",
        "mcp_capability_discovery",
        ["tenant_id", "definition_id", sa.text("discovered_at DESC")],
    )
    op.create_index(
        "ix_mcp_capability_discovery__tenant_content_hash_status",
        "mcp_capability_discovery",
        ["tenant_id", "content_hash", "status"],
    )
    op.execute(
        sa.text("ALTER TABLE mcp_capability_discovery ENABLE ROW LEVEL SECURITY")
    )
    op.execute(sa.text("ALTER TABLE mcp_capability_discovery FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON mcp_capability_discovery "
            f"USING ({TENANT_POLICY_EXPRESSION}) "
            f"WITH CHECK ({TENANT_POLICY_EXPRESSION})"
        )
    )
    op.execute(sa.text(_DISCOVERY_GUARD_FUNCTION))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_mcp_capability_discovery__guard "
            "BEFORE UPDATE OR DELETE ON mcp_capability_discovery "
            "FOR EACH ROW EXECUTE FUNCTION guard_mcp_capability_discovery()"
        )
    )
    for action in MCP_PERMISSIONS:
        op.execute(sa.text(f"""DO $$ DECLARE tenant_uuid uuid; BEGIN
                FOR tenant_uuid IN SELECT tenant_id FROM role
                WHERE built_in IS TRUE AND code = 'tenant_admin' LOOP
                    PERFORM set_config(
                        'app.current_tenant_id', tenant_uuid::text, true
                    );
                    INSERT INTO role_permission
                        (tenant_id, role_id, resource_type, action)
                    SELECT tenant_id, id, 'mcp', '{action}' FROM role
                    WHERE tenant_id = tenant_uuid AND built_in IS TRUE
                    AND code = 'tenant_admin' ON CONFLICT DO NOTHING;
                END LOOP; END $$"""))


def downgrade() -> None:
    for action in MCP_PERMISSIONS:
        op.execute(sa.text(f"""DO $$ DECLARE tenant_uuid uuid; BEGIN
                FOR tenant_uuid IN SELECT tenant_id FROM role
                WHERE built_in IS TRUE AND code = 'tenant_admin' LOOP
                    PERFORM set_config(
                        'app.current_tenant_id', tenant_uuid::text, true
                    );
                    DELETE FROM role_permission WHERE tenant_id = tenant_uuid
                    AND resource_type = 'mcp' AND action = '{action}';
                END LOOP; END $$"""))
    op.execute(
        sa.text(
            "DROP TRIGGER trg_mcp_capability_discovery__guard "
            "ON mcp_capability_discovery"
        )
    )
    op.execute(sa.text("DROP FUNCTION guard_mcp_capability_discovery()"))
    op.execute(
        sa.text("DROP POLICY IF EXISTS tenant_isolation ON mcp_capability_discovery")
    )
    op.execute(
        sa.text("ALTER TABLE mcp_capability_discovery DISABLE ROW LEVEL SECURITY")
    )
    op.drop_index(
        "ix_mcp_capability_discovery__tenant_content_hash_status",
        table_name="mcp_capability_discovery",
    )
    op.drop_index(
        "ix_mcp_capability_discovery__tenant_definition_discovered_at",
        table_name="mcp_capability_discovery",
    )
    op.drop_table("mcp_capability_discovery")


_DISCOVERY_GUARD_FUNCTION = """
CREATE FUNCTION guard_mcp_capability_discovery() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'MCP capability discovery cannot be deleted';
    END IF;
    IF ROW(NEW.id, NEW.tenant_id, NEW.definition_id, NEW.operation_id,
           NEW.draft_resource_version, NEW.content_hash, NEW.status,
           NEW.protocol_version, NEW.server_name, NEW.server_version,
           NEW.tools_json, NEW.capability_hash, NEW.findings_json,
           NEW.discovered_at, NEW.discovered_by, NEW.source_discovery_id)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.tenant_id, OLD.definition_id, OLD.operation_id,
           OLD.draft_resource_version, OLD.content_hash, OLD.status,
           OLD.protocol_version, OLD.server_name, OLD.server_version,
           OLD.tools_json, OLD.capability_hash, OLD.findings_json,
           OLD.discovered_at, OLD.discovered_by, OLD.source_discovery_id) THEN
        RAISE EXCEPTION 'MCP capability discovery evidence is immutable';
    END IF;
    IF OLD.published_version_id IS NOT NULL OR NEW.published_version_id IS NULL THEN
        RAISE EXCEPTION 'MCP discovery publication binding is one-time';
    END IF;
    RETURN NEW;
END; $$
"""

"""Harden AuditLog retention and add the frozen tenant-scoped query path.

Revision ID: 0029_audit_query_retention
Revises: 0028_execution_ticket_gateway
Create Date: 2026-08-10
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029_audit_query_retention"
down_revision: str | None = "0028_execution_ticket_gateway"
branch_labels: str | None = None
depends_on: str | None = None

AUDIT_POLICY_EXPRESSION = (
    "current_setting('app.platform_context', true) = 'true' OR "
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)


def upgrade() -> None:
    op.add_column(
        "audit_log",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE audit_log SET run_id = resource_id "
            "WHERE run_id IS NULL AND resource_type = 'run' "
            "AND resource_id IS NOT NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE audit_log SET run_id = (metadata_json->>'run_id')::uuid "
            "WHERE run_id IS NULL AND metadata_json ? 'run_id' "
            "AND metadata_json->>'run_id' ~ "
            "'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
            "[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$'"
        )
    )
    op.create_index(
        "ix_audit_log__tenant_created_id",
        "audit_log",
        ["tenant_id", "created_at", "id"],
    )
    op.create_index(
        "ix_audit_log__tenant_run_created_id",
        "audit_log",
        ["tenant_id", "run_id", "created_at", "id"],
    )
    op.execute(sa.text("ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE audit_log FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON audit_log "
            f"USING ({AUDIT_POLICY_EXPRESSION}) "
            f"WITH CHECK ({AUDIT_POLICY_EXPRESSION})"
        )
    )
    op.execute(sa.text(_AUDIT_GUARD_FUNCTION))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_audit_log__guard "
            "BEFORE UPDATE OR DELETE ON audit_log FOR EACH ROW "
            "EXECUTE FUNCTION guard_audit_log()"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO role_permission (tenant_id, role_id, resource_type, action) "
            "SELECT tenant_id, id, 'audit', 'list' FROM role "
            "WHERE built_in IS TRUE AND code = 'tenant_admin' "
            "ON CONFLICT DO NOTHING"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM role_permission WHERE resource_type = 'audit' "
            "AND action = 'list' AND role_id IN "
            "(SELECT id FROM role WHERE built_in IS TRUE AND code = 'tenant_admin')"
        )
    )
    op.execute(sa.text("DROP TRIGGER trg_audit_log__guard ON audit_log"))
    op.execute(sa.text("DROP FUNCTION guard_audit_log()"))
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON audit_log"))
    op.execute(sa.text("ALTER TABLE audit_log DISABLE ROW LEVEL SECURITY"))
    op.drop_index("ix_audit_log__tenant_run_created_id", table_name="audit_log")
    op.drop_index("ix_audit_log__tenant_created_id", table_name="audit_log")
    op.drop_column("audit_log", "run_id")


_AUDIT_GUARD_FUNCTION = """
CREATE FUNCTION guard_audit_log() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit log facts are immutable and retained';
END;
$$;
"""

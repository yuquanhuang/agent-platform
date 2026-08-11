"""Add canonical, tenant-isolated Workspace metadata and quotas.

Revision ID: 0022_workspace_isolation
Revises: 0021_sandbox_lifecycle
Create Date: 2026-08-09
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022_workspace_isolation"
down_revision: str | None = "0021_sandbox_lifecycle"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY_EXPRESSION = (
    "tenant_id = nullif(current_setting('app.current_tenant_id', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        "workspace",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("uri", sa.String(length=4096), nullable=False),
        sa.Column("quota_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "used_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("max_files", sa.BigInteger(), nullable=False),
        sa.Column(
            "file_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("max_file_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'ACTIVE'"),
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
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("quota_bytes > 0", name="quota_bytes"),
        sa.CheckConstraint(
            "used_bytes >= 0 AND used_bytes <= quota_bytes", name="used_bytes"
        ),
        sa.CheckConstraint("max_files > 0", name="max_files"),
        sa.CheckConstraint(
            "file_count >= 0 AND file_count <= max_files", name="file_count"
        ),
        sa.CheckConstraint(
            "max_file_bytes > 0 AND max_file_bytes <= quota_bytes",
            name="max_file_bytes",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','SEALED','QUARANTINED','DELETING','DELETED')",
            name="status",
        ),
        sa.CheckConstraint("expires_at > created_at", name="expires_at"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_workspace__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "session_id"],
            ["agent_run.tenant_id", "agent_run.id", "agent_run.session_id"],
            name="fk_workspace__tenant_run_session__agent_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["tenant_member.tenant_id", "tenant_member.user_id"],
            name="fk_workspace__tenant_user__tenant_member",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workspace"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_workspace__tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "uri", name="uq_workspace__tenant_uri"),
        sa.UniqueConstraint("tenant_id", "run_id", name="uq_workspace__tenant_run"),
    )
    op.create_index(
        "ix_workspace__tenant_status_expires_at",
        "workspace",
        ["tenant_id", "status", "expires_at"],
    )
    op.create_index(
        "ix_workspace__tenant_session_created_at",
        "workspace",
        ["tenant_id", "session_id", "created_at"],
    )
    op.execute(sa.text(_BACKFILL_WORKSPACES))
    op.create_foreign_key(
        "fk_sandbox_instance__tenant_workspace__workspace",
        "sandbox_instance",
        "workspace",
        ["tenant_id", "workspace_uri"],
        ["tenant_id", "uri"],
        ondelete="RESTRICT",
    )
    op.execute(sa.text("ALTER TABLE workspace ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE workspace FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON workspace "
            f"USING ({TENANT_POLICY_EXPRESSION}) "
            f"WITH CHECK ({TENANT_POLICY_EXPRESSION})"
        )
    )
    op.execute(sa.text(_WORKSPACE_GUARD_FUNCTION))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_workspace__guard BEFORE UPDATE OR DELETE "
            "ON workspace FOR EACH ROW EXECUTE FUNCTION guard_workspace()"
        )
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_sandbox_instance__tenant_workspace__workspace",
        "sandbox_instance",
        type_="foreignkey",
    )
    op.execute(sa.text("DROP TRIGGER trg_workspace__guard ON workspace"))
    op.execute(sa.text("DROP FUNCTION guard_workspace()"))
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON workspace"))
    op.execute(sa.text("ALTER TABLE workspace DISABLE ROW LEVEL SECURITY"))
    op.drop_index("ix_workspace__tenant_session_created_at", table_name="workspace")
    op.drop_index("ix_workspace__tenant_status_expires_at", table_name="workspace")
    op.drop_table("workspace")


_BACKFILL_WORKSPACES = """
INSERT INTO workspace (
    id, tenant_id, user_id, session_id, run_id, uri,
    quota_bytes, used_bytes, max_files, file_count, max_file_bytes,
    status, created_at, updated_at, expires_at
)
SELECT
    gen_random_uuid(), tenant_id, user_id, session_id, run_id, workspace_uri,
    (policy_json ->> 'disk_mb')::bigint * 1048576,
    0,
    (policy_json -> 'filesystem' ->> 'max_files')::bigint,
    0,
    (policy_json -> 'filesystem' ->> 'max_file_bytes')::bigint,
    CASE
        WHEN status = 'QUARANTINED' THEN 'QUARANTINED'
        WHEN status IN ('FAILED', 'TERMINATING', 'TERMINATED') THEN 'SEALED'
        ELSE 'ACTIVE'
    END,
    created_at,
    updated_at,
    created_at + interval '7 days'
FROM sandbox_instance
ON CONFLICT (tenant_id, run_id) DO NOTHING
"""


_WORKSPACE_GUARD_FUNCTION = """
CREATE FUNCTION guard_workspace() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'workspace cannot be deleted';
    END IF;
    IF ROW(NEW.id, NEW.tenant_id, NEW.user_id, NEW.session_id, NEW.run_id,
           NEW.uri, NEW.quota_bytes, NEW.max_files, NEW.max_file_bytes,
           NEW.created_at, NEW.expires_at)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.tenant_id, OLD.user_id, OLD.session_id, OLD.run_id,
           OLD.uri, OLD.quota_bytes, OLD.max_files, OLD.max_file_bytes,
           OLD.created_at, OLD.expires_at) THEN
        RAISE EXCEPTION 'workspace immutable identity or quota cannot change';
    END IF;
    IF NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'workspace updated_at cannot move backwards';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'ACTIVE' AND NEW.status IN ('SEALED', 'QUARANTINED', 'DELETING')) OR
        (OLD.status = 'SEALED' AND NEW.status IN ('QUARANTINED', 'DELETING')) OR
        (OLD.status = 'QUARANTINED' AND NEW.status = 'DELETING') OR
        (OLD.status = 'DELETING' AND NEW.status IN ('DELETED', 'QUARANTINED'))
    ) THEN
        RAISE EXCEPTION 'workspace lifecycle transition is not allowed';
    END IF;
    RETURN NEW;
END; $$
"""

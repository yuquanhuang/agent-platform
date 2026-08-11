"""Add tenant-isolated Artifact upload and scan metadata.

Revision ID: 0023_artifact_upload_scan
Revises: 0022_workspace_isolation
Create Date: 2026-08-09
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023_artifact_upload_scan"
down_revision: str | None = "0022_workspace_isolation"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY_EXPRESSION = (
    "tenant_id = nullif(current_setting('app.current_tenant_id', true), '')::uuid"
)
ARTIFACT_PERMISSIONS = ("create", "read")


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_workspace__tenant_id_run_user",
        "workspace",
        ["tenant_id", "id", "run_id", "user_id"],
    )
    op.create_table(
        "artifact",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("quarantine_object_uri", sa.String(length=2048), nullable=False),
        sa.Column("object_uri", sa.String(length=2048), nullable=True),
        sa.Column("content_hash", sa.String(length=80), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'UPLOADING'"),
            nullable=False,
        ),
        sa.Column(
            "required_output",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("scan_result_json", postgresql.JSONB(), nullable=True),
        sa.Column("upload_expires_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(workspace_id IS NULL AND run_id IS NULL) OR "
            "(workspace_id IS NOT NULL AND run_id IS NOT NULL)",
            name="workspace_run_binding",
        ),
        sa.CheckConstraint(
            "size_bytes > 0 AND size_bytes <= 104857600", name="size_bytes"
        ),
        sa.CheckConstraint(
            "content_hash ~ '^sha256:[a-f0-9]{64}$'", name="content_hash"
        ),
        sa.CheckConstraint(
            "status IN ('UPLOADING','SCANNING','AVAILABLE','REJECTED','FAILED',"
            "'EXPIRED','DELETING','DELETED')",
            name="status",
        ),
        sa.CheckConstraint(
            "(status IN ('AVAILABLE','EXPIRED','DELETING','DELETED') "
            "AND object_uri IS NOT NULL) OR "
            "(status IN ('UPLOADING','SCANNING','REJECTED','FAILED') "
            "AND object_uri IS NULL)",
            name="trusted_object_status",
        ),
        sa.CheckConstraint(
            "scan_result_json IS NULL OR jsonb_typeof(scan_result_json) = 'object'",
            name="scan_result_json",
        ),
        sa.CheckConstraint(
            "(status IN ('AVAILABLE','REJECTED','FAILED') "
            "AND scan_result_json IS NOT NULL) OR "
            "status NOT IN ('AVAILABLE','REJECTED','FAILED')",
            name="scan_result_status",
        ),
        sa.CheckConstraint("upload_expires_at > created_at", name="upload_expires_at"),
        sa.CheckConstraint("expires_at > created_at", name="expires_at"),
        sa.CheckConstraint(
            "(status = 'DELETED' AND deleted_at IS NOT NULL) OR "
            "(status <> 'DELETED' AND deleted_at IS NULL)",
            name="deleted_at_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_artifact__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_user_id"],
            ["tenant_member.tenant_id", "tenant_member.user_id"],
            name="fk_artifact__tenant_owner__tenant_member",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "run_id", "owner_user_id"],
            [
                "workspace.tenant_id",
                "workspace.id",
                "workspace.run_id",
                "workspace.user_id",
            ],
            name="fk_artifact__tenant_workspace_run_owner__workspace",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_artifact"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_artifact__tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "quarantine_object_uri",
            name="uq_artifact__tenant_quarantine_object_uri",
        ),
        sa.UniqueConstraint(
            "tenant_id", "object_uri", name="uq_artifact__tenant_object_uri"
        ),
    )
    op.create_index(
        "ix_artifact__tenant_owner_created_at",
        "artifact",
        ["tenant_id", "owner_user_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_artifact__tenant_run_status",
        "artifact",
        ["tenant_id", "run_id", "status"],
    )
    op.create_index(
        "ix_artifact__tenant_status_expires_at",
        "artifact",
        ["tenant_id", "status", "expires_at"],
    )
    op.execute(sa.text("ALTER TABLE artifact ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE artifact FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON artifact "
            f"USING ({TENANT_POLICY_EXPRESSION}) "
            f"WITH CHECK ({TENANT_POLICY_EXPRESSION})"
        )
    )
    op.execute(sa.text(_ARTIFACT_GUARD_FUNCTION))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_artifact__guard BEFORE UPDATE OR DELETE "
            "ON artifact FOR EACH ROW EXECUTE FUNCTION guard_artifact()"
        )
    )
    for action in ARTIFACT_PERMISSIONS:
        op.execute(sa.text(f"""DO $$ DECLARE tenant_uuid uuid; BEGIN
                FOR tenant_uuid IN SELECT tenant_id FROM role
                WHERE built_in IS TRUE AND code = 'tenant_admin' LOOP
                    PERFORM set_config(
                        'app.current_tenant_id', tenant_uuid::text, true
                    );
                    INSERT INTO role_permission
                        (tenant_id, role_id, resource_type, action)
                    SELECT tenant_id, id, 'artifact', '{action}' FROM role
                    WHERE tenant_id = tenant_uuid AND built_in IS TRUE
                    AND code = 'tenant_admin' ON CONFLICT DO NOTHING;
                END LOOP; END $$"""))


def downgrade() -> None:
    for action in ARTIFACT_PERMISSIONS:
        op.execute(sa.text(f"""DO $$ DECLARE tenant_uuid uuid; BEGIN
                FOR tenant_uuid IN SELECT tenant_id FROM role
                WHERE built_in IS TRUE AND code = 'tenant_admin' LOOP
                    PERFORM set_config(
                        'app.current_tenant_id', tenant_uuid::text, true
                    );
                    DELETE FROM role_permission WHERE tenant_id = tenant_uuid
                    AND resource_type = 'artifact' AND action = '{action}';
                END LOOP; END $$"""))
    op.execute(sa.text("DROP TRIGGER trg_artifact__guard ON artifact"))
    op.execute(sa.text("DROP FUNCTION guard_artifact()"))
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON artifact"))
    op.execute(sa.text("ALTER TABLE artifact DISABLE ROW LEVEL SECURITY"))
    op.drop_index("ix_artifact__tenant_status_expires_at", table_name="artifact")
    op.drop_index("ix_artifact__tenant_run_status", table_name="artifact")
    op.drop_index("ix_artifact__tenant_owner_created_at", table_name="artifact")
    op.drop_table("artifact")
    op.drop_constraint("uq_workspace__tenant_id_run_user", "workspace", type_="unique")


_ARTIFACT_GUARD_FUNCTION = """
CREATE FUNCTION guard_artifact() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'artifact cannot be deleted';
    END IF;
    IF ROW(NEW.id, NEW.tenant_id, NEW.workspace_id, NEW.run_id,
           NEW.owner_user_id, NEW.name, NEW.quarantine_object_uri,
           NEW.content_hash, NEW.size_bytes, NEW.content_type,
           NEW.required_output, NEW.upload_expires_at, NEW.created_at,
           NEW.expires_at)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.tenant_id, OLD.workspace_id, OLD.run_id,
           OLD.owner_user_id, OLD.name, OLD.quarantine_object_uri,
           OLD.content_hash, OLD.size_bytes, OLD.content_type,
           OLD.required_output, OLD.upload_expires_at, OLD.created_at,
           OLD.expires_at) THEN
        RAISE EXCEPTION 'artifact immutable metadata cannot change';
    END IF;
    IF NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'artifact updated_at cannot move backwards';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'UPLOADING' AND NEW.status IN ('SCANNING', 'FAILED')) OR
        (OLD.status = 'SCANNING' AND NEW.status IN ('AVAILABLE', 'REJECTED', 'FAILED')) OR
        (OLD.status = 'AVAILABLE' AND NEW.status IN ('EXPIRED', 'DELETING')) OR
        (OLD.status IN ('REJECTED', 'FAILED', 'EXPIRED') AND NEW.status = 'DELETING') OR
        (OLD.status = 'DELETING' AND NEW.status = 'DELETED')
    ) THEN
        RAISE EXCEPTION 'artifact lifecycle transition is not allowed';
    END IF;
    RETURN NEW;
END; $$
"""

"""Add revocable Artifact Download Gateway grants.

Revision ID: 0032_artifact_download_gateway
Revises: 0031_sandbox_reconcile_index
Create Date: 2026-08-11
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0032_artifact_download_gateway"
down_revision: str | None = "0031_sandbox_reconcile_index"
branch_labels: str | None = None
depends_on: str | None = None

GRANT_POLICY_EXPRESSION = (
    "current_setting('app.platform_context', true) = 'true' OR "
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        "artifact_download_grant",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=80), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("token_hash ~ '^sha256:[a-f0-9]{64}$'", name="token_hash"),
        sa.CheckConstraint("expires_at > created_at", name="expires_at"),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at", name="revoked_at"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_artifact_download_grant__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "artifact_id"],
            ["artifact.tenant_id", "artifact.id"],
            name="fk_artifact_download_grant__tenant_artifact__artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_user_id"],
            ["tenant_member.tenant_id", "tenant_member.user_id"],
            name="fk_artifact_download_grant__tenant_owner__tenant_member",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_artifact_download_grant"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_artifact_download_grant__tenant_id_id"
        ),
        sa.UniqueConstraint(
            "token_hash", name="uq_artifact_download_grant__token_hash"
        ),
    )
    op.create_index(
        "ix_artifact_download_grant__tenant_artifact_expires_at",
        "artifact_download_grant",
        ["tenant_id", "artifact_id", "expires_at"],
    )
    op.execute(sa.text("ALTER TABLE artifact_download_grant ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE artifact_download_grant FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON artifact_download_grant "
            f"USING ({GRANT_POLICY_EXPRESSION}) "
            f"WITH CHECK ({GRANT_POLICY_EXPRESSION})"
        )
    )
    op.execute(sa.text(_GRANT_GUARD_FUNCTION))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_artifact_download_grant__guard "
            "BEFORE UPDATE OR DELETE ON artifact_download_grant FOR EACH ROW "
            "EXECUTE FUNCTION guard_artifact_download_grant()"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER trg_artifact_download_grant__guard "
            "ON artifact_download_grant"
        )
    )
    op.execute(sa.text("DROP FUNCTION guard_artifact_download_grant()"))
    op.execute(
        sa.text("DROP POLICY IF EXISTS tenant_isolation ON artifact_download_grant")
    )
    op.execute(
        sa.text("ALTER TABLE artifact_download_grant DISABLE ROW LEVEL SECURITY")
    )
    op.drop_index(
        "ix_artifact_download_grant__tenant_artifact_expires_at",
        table_name="artifact_download_grant",
    )
    op.drop_table("artifact_download_grant")


_GRANT_GUARD_FUNCTION = """
CREATE FUNCTION guard_artifact_download_grant() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'artifact download grants are retained facts';
    END IF;
    IF ROW(NEW.id, NEW.tenant_id, NEW.artifact_id, NEW.owner_user_id,
           NEW.token_hash, NEW.expires_at, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.tenant_id, OLD.artifact_id, OLD.owner_user_id,
           OLD.token_hash, OLD.expires_at, OLD.created_at) THEN
        RAISE EXCEPTION 'artifact download grant bindings are immutable';
    END IF;
    IF OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL THEN
        RAISE EXCEPTION 'artifact download grant can only be revoked once';
    END IF;
    IF NEW.revoked_at < OLD.created_at THEN
        RAISE EXCEPTION 'artifact download grant revoked_at is invalid';
    END IF;
    RETURN NEW;
END;
$$;
"""

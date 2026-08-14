"""Add frozen Artifact retention deletion facts and legal holds.

Revision ID: 0040_artifact_retention
Revises: 0039_storage_policy
Create Date: 2026-08-13
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040_artifact_retention"
down_revision: str | None = "0039_storage_policy"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY = (
    "current_setting('app.platform_context', true) = 'true' OR "
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)


def upgrade() -> None:
    op.add_column(
        "artifact",
        sa.Column("retention_delete_after", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE artifact SET retention_delete_after = CASE "
            "WHEN status IN ('FAILED','REJECTED') THEN updated_at + interval '7 days' "
            "WHEN status = 'EXPIRED' THEN greatest(updated_at, expires_at) + interval '7 days' "
            "ELSE NULL END"
        )
    )
    op.create_check_constraint(
        "retention_delete_after_status",
        "artifact",
        "(status IN ('REJECTED','FAILED','EXPIRED') "
        "AND retention_delete_after IS NOT NULL) OR "
        "(status IN ('UPLOADING','SCANNING','AVAILABLE') "
        "AND retention_delete_after IS NULL) OR status IN ('DELETING','DELETED')",
    )
    op.create_index(
        "ix_artifact__tenant_status_retention_delete_after",
        "artifact",
        ["tenant_id", "status", "retention_delete_after"],
    )
    op.create_table(
        "artifact_legal_hold",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_ref", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.String(length=2000), nullable=False),
        sa.Column("placed_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("placed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(case_ref) BETWEEN 1 AND 255", name="case_ref"),
        sa.CheckConstraint("length(reason) BETWEEN 1 AND 2000", name="reason"),
        sa.CheckConstraint(
            "(released_at IS NULL AND released_by IS NULL) OR "
            "(released_at IS NOT NULL AND released_by IS NOT NULL "
            "AND released_at >= placed_at)",
            name="release_fact",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "artifact_id"],
            ["artifact.tenant_id", "artifact.id"],
            name="fk_artifact_legal_hold__tenant_artifact",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_artifact_legal_hold"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_artifact_legal_hold__tenant_id"
        ),
    )
    op.create_index(
        "ix_artifact_legal_hold__tenant_artifact_active",
        "artifact_legal_hold",
        ["tenant_id", "artifact_id"],
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.create_index(
        "uq_artifact_legal_hold__tenant_artifact_case_active",
        "artifact_legal_hold",
        ["tenant_id", "artifact_id", "case_ref"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.execute(sa.text("ALTER TABLE artifact_legal_hold ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE artifact_legal_hold FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON artifact_legal_hold "
            f"USING ({TENANT_POLICY}) WITH CHECK ({TENANT_POLICY})"
        )
    )
    op.execute(sa.text(_HOLD_GUARD))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_artifact_legal_hold__guard BEFORE UPDATE OR DELETE "
            "ON artifact_legal_hold FOR EACH ROW "
            "EXECUTE FUNCTION guard_artifact_legal_hold()"
        )
    )
    op.execute(sa.text(_RETENTION_GUARD))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_artifact__retention_guard BEFORE UPDATE "
            "ON artifact FOR EACH ROW EXECUTE FUNCTION guard_artifact_retention()"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER trg_artifact__retention_guard ON artifact"))
    op.execute(sa.text("DROP FUNCTION guard_artifact_retention()"))
    op.execute(
        sa.text("DROP TRIGGER trg_artifact_legal_hold__guard ON artifact_legal_hold")
    )
    op.execute(sa.text("DROP FUNCTION guard_artifact_legal_hold()"))
    op.execute(sa.text("DROP POLICY tenant_isolation ON artifact_legal_hold"))
    op.execute(sa.text("ALTER TABLE artifact_legal_hold DISABLE ROW LEVEL SECURITY"))
    op.drop_index(
        "uq_artifact_legal_hold__tenant_artifact_case_active",
        table_name="artifact_legal_hold",
    )
    op.drop_index(
        "ix_artifact_legal_hold__tenant_artifact_active",
        table_name="artifact_legal_hold",
    )
    op.drop_table("artifact_legal_hold")
    op.drop_index(
        "ix_artifact__tenant_status_retention_delete_after", table_name="artifact"
    )
    op.drop_constraint("retention_delete_after_status", "artifact", type_="check")
    op.drop_column("artifact", "retention_delete_after")


_HOLD_GUARD = """
CREATE FUNCTION guard_artifact_legal_hold() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'artifact legal hold cannot be deleted';
    END IF;
    IF ROW(NEW.id, NEW.tenant_id, NEW.artifact_id, NEW.case_ref, NEW.reason,
           NEW.placed_by, NEW.placed_at)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.tenant_id, OLD.artifact_id, OLD.case_ref, OLD.reason,
           OLD.placed_by, OLD.placed_at) THEN
        RAISE EXCEPTION 'artifact legal hold placement is immutable';
    END IF;
    IF OLD.released_at IS NOT NULL OR
       (NEW.released_at IS NULL) <> (NEW.released_by IS NULL) THEN
        RAISE EXCEPTION 'artifact legal hold release is invalid';
    END IF;
    RETURN NEW;
END; $$;
"""

_RETENTION_GUARD = """
CREATE FUNCTION guard_artifact_retention() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.retention_delete_after IS DISTINCT FROM OLD.retention_delete_after AND (
        OLD.retention_delete_after IS NOT NULL OR
        NEW.retention_delete_after IS NULL OR
        NEW.status NOT IN ('FAILED','REJECTED','EXPIRED')
    ) THEN
        RAISE EXCEPTION 'artifact retention deletion fact is immutable';
    END IF;
    RETURN NEW;
END; $$;
"""

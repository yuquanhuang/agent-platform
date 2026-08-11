"""Add immutable Skill supply-chain scan evidence and permissions.

Revision ID: 0025_skill_supply_chain_scan
Revises: 0024_artifact_download_delete
Create Date: 2026-08-10
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025_skill_supply_chain_scan"
down_revision: str | None = "0024_artifact_download_delete"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY_EXPRESSION = (
    "tenant_id = nullif(current_setting('app.current_tenant_id', true), '')::uuid"
)
SKILL_PERMISSIONS = (
    "create",
    "read",
    "list",
    "update",
    "delete",
    "publish",
    "rollback",
    "disable",
)


def upgrade() -> None:
    op.create_table(
        "skill_supply_chain_scan",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("definition_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("draft_resource_version", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.String(length=80), nullable=False),
        sa.Column("scanner_name", sa.String(length=128), nullable=False),
        sa.Column("scanner_version", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "findings_json",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("report_hash", sa.String(length=80), nullable=False),
        sa.Column("sbom_json", postgresql.JSONB(), nullable=False),
        sa.Column("sbom_hash", sa.String(length=80), nullable=False),
        sa.Column("signature_status", sa.String(length=20), nullable=False),
        sa.Column("provenance_status", sa.String(length=20), nullable=False),
        sa.Column(
            "scanned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("scanned_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("published_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "draft_resource_version >= 1", name="draft_resource_version"
        ),
        sa.CheckConstraint("status IN ('PASSED','REJECTED','FAILED')", name="status"),
        sa.CheckConstraint(
            "content_hash ~ '^sha256:[a-f0-9]{64}$'", name="content_hash"
        ),
        sa.CheckConstraint("report_hash ~ '^sha256:[a-f0-9]{64}$'", name="report_hash"),
        sa.CheckConstraint("sbom_hash ~ '^sha256:[a-f0-9]{64}$'", name="sbom_hash"),
        sa.CheckConstraint(
            "signature_status IN ('VERIFIED','UNVERIFIED','NOT_PROVIDED')",
            name="signature_status",
        ),
        sa.CheckConstraint(
            "provenance_status IN ('VERIFIED','UNVERIFIED','NOT_PROVIDED')",
            name="provenance_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(findings_json) = 'array'", name="findings_json"
        ),
        sa.CheckConstraint("jsonb_typeof(sbom_json) = 'object'", name="sbom_json"),
        sa.CheckConstraint(
            "published_version_id IS NULL OR status = 'PASSED'",
            name="published_version_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_skill_supply_chain_scan__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "definition_id"],
            ["resource_definition.tenant_id", "resource_definition.id"],
            name="fk_skill_supply_chain_scan__tenant_definition",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "published_version_id"],
            ["resource_version.tenant_id", "resource_version.id"],
            name="fk_skill_supply_chain_scan__tenant_published_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scanned_by"],
            ["app_user.id"],
            name="fk_skill_supply_chain_scan__scanned_by__app_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_skill_supply_chain_scan"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_skill_supply_chain_scan__tenant_id_id"
        ),
        sa.UniqueConstraint(
            "published_version_id",
            name="uq_skill_supply_chain_scan__published_version_id",
        ),
    )
    op.create_index(
        "ix_skill_supply_chain_scan__tenant_definition_scanned_at",
        "skill_supply_chain_scan",
        ["tenant_id", "definition_id", sa.text("scanned_at DESC")],
    )
    op.create_index(
        "ix_skill_supply_chain_scan__tenant_content_hash_status",
        "skill_supply_chain_scan",
        ["tenant_id", "content_hash", "status"],
    )
    op.execute(sa.text("ALTER TABLE skill_supply_chain_scan ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE skill_supply_chain_scan FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON skill_supply_chain_scan "
            f"USING ({TENANT_POLICY_EXPRESSION}) "
            f"WITH CHECK ({TENANT_POLICY_EXPRESSION})"
        )
    )
    op.execute(sa.text(_SCAN_GUARD_FUNCTION))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_skill_supply_chain_scan__guard "
            "BEFORE UPDATE OR DELETE ON skill_supply_chain_scan "
            "FOR EACH ROW EXECUTE FUNCTION guard_skill_supply_chain_scan()"
        )
    )
    for action in SKILL_PERMISSIONS:
        op.execute(sa.text(f"""DO $$ DECLARE tenant_uuid uuid; BEGIN
                FOR tenant_uuid IN SELECT tenant_id FROM role
                WHERE built_in IS TRUE AND code = 'tenant_admin' LOOP
                    PERFORM set_config(
                        'app.current_tenant_id', tenant_uuid::text, true
                    );
                    INSERT INTO role_permission
                        (tenant_id, role_id, resource_type, action)
                    SELECT tenant_id, id, 'skill', '{action}' FROM role
                    WHERE tenant_id = tenant_uuid AND built_in IS TRUE
                    AND code = 'tenant_admin' ON CONFLICT DO NOTHING;
                END LOOP; END $$"""))


def downgrade() -> None:
    for action in SKILL_PERMISSIONS:
        op.execute(sa.text(f"""DO $$ DECLARE tenant_uuid uuid; BEGIN
                FOR tenant_uuid IN SELECT tenant_id FROM role
                WHERE built_in IS TRUE AND code = 'tenant_admin' LOOP
                    PERFORM set_config(
                        'app.current_tenant_id', tenant_uuid::text, true
                    );
                    DELETE FROM role_permission WHERE tenant_id = tenant_uuid
                    AND resource_type = 'skill' AND action = '{action}';
                END LOOP; END $$"""))
    op.execute(
        sa.text(
            "DROP TRIGGER trg_skill_supply_chain_scan__guard "
            "ON skill_supply_chain_scan"
        )
    )
    op.execute(sa.text("DROP FUNCTION guard_skill_supply_chain_scan()"))
    op.execute(
        sa.text("DROP POLICY IF EXISTS tenant_isolation ON skill_supply_chain_scan")
    )
    op.execute(
        sa.text("ALTER TABLE skill_supply_chain_scan DISABLE ROW LEVEL SECURITY")
    )
    op.drop_index(
        "ix_skill_supply_chain_scan__tenant_content_hash_status",
        table_name="skill_supply_chain_scan",
    )
    op.drop_index(
        "ix_skill_supply_chain_scan__tenant_definition_scanned_at",
        table_name="skill_supply_chain_scan",
    )
    op.drop_table("skill_supply_chain_scan")


_SCAN_GUARD_FUNCTION = """
CREATE FUNCTION guard_skill_supply_chain_scan() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'skill supply-chain scan cannot be deleted';
    END IF;
    IF ROW(NEW.id, NEW.tenant_id, NEW.definition_id,
           NEW.draft_resource_version, NEW.content_hash, NEW.scanner_name,
           NEW.scanner_version, NEW.policy_version, NEW.status,
           NEW.findings_json, NEW.report_hash, NEW.sbom_json, NEW.sbom_hash,
           NEW.signature_status, NEW.provenance_status, NEW.scanned_at,
           NEW.scanned_by)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.tenant_id, OLD.definition_id,
           OLD.draft_resource_version, OLD.content_hash, OLD.scanner_name,
           OLD.scanner_version, OLD.policy_version, OLD.status,
           OLD.findings_json, OLD.report_hash, OLD.sbom_json, OLD.sbom_hash,
           OLD.signature_status, OLD.provenance_status, OLD.scanned_at,
           OLD.scanned_by) THEN
        RAISE EXCEPTION 'skill supply-chain scan evidence is immutable';
    END IF;
    IF OLD.published_version_id IS NOT NULL OR NEW.published_version_id IS NULL THEN
        RAISE EXCEPTION 'skill scan publication binding is one-time';
    END IF;
    RETURN NEW;
END; $$
"""

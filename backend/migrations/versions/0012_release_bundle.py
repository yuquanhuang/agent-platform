"""Add Release workflow and Runtime Bundle persistence.

Revision ID: 0012_release_bundle
Revises: 0011_agent_snapshot
Create Date: 2026-08-07
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_release_bundle"
down_revision: str | None = "0011_agent_snapshot"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY_EXPRESSION = (
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        "release",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expected_agent_version", sa.BigInteger(), nullable=False),
        sa.Column("runtime_targets_json", postgresql.JSONB(), nullable=False),
        sa.Column("release_note", sa.String(length=2000), nullable=False),
        sa.Column("run_smoke_test", sa.Boolean(), nullable=False),
        sa.Column("activate_on_success", sa.Boolean(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'REQUESTED'"),
            nullable=False,
        ),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "deployment_ids_json",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_detail_json", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "expected_agent_version >= 1", name="expected_agent_version"
        ),
        sa.CheckConstraint(
            "status IN ('REQUESTED', 'VALIDATING', 'COMPILING', 'SCANNING', "
            "'SMOKE_TESTING', 'ACTIVATING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(runtime_targets_json) = 'array' "
            "AND jsonb_array_length(runtime_targets_json) >= 1",
            name="runtime_targets_json",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(deployment_ids_json) = 'array'",
            name="deployment_ids_json",
        ),
        sa.CheckConstraint(
            "error_detail_json IS NULL OR jsonb_typeof(error_detail_json) = 'object'",
            name="error_detail_json",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_release__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agent_definition.tenant_id", "agent_definition.id"],
            name="fk_release__tenant_agent__agent_definition",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "snapshot_id"],
            ["agent_snapshot.tenant_id", "agent_snapshot.id"],
            name="fk_release__tenant_snapshot__agent_snapshot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["app_user.id"],
            name="fk_release__requested_by__app_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operation_record.id"],
            name="fk_release__operation_id__operation_record",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_release"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_release__tenant_id_id"),
        sa.UniqueConstraint("operation_id", name="uq_release__operation_id"),
        sa.UniqueConstraint("workflow_id", name="uq_release__workflow_id"),
    )
    op.create_index(
        "ix_release__tenant_agent_created_at",
        "release",
        ["tenant_id", "agent_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_release__tenant_status_created_at",
        "release",
        ["tenant_id", "status", "created_at"],
        unique=False,
    )

    op.create_table(
        "runtime_bundle",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("runtime_type", sa.String(length=64), nullable=False),
        sa.Column("compiler_name", sa.String(length=128), nullable=False),
        sa.Column("compiler_version", sa.String(length=64), nullable=False),
        sa.Column("manifest_schema_version", sa.String(length=32), nullable=False),
        sa.Column("manifest_json", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(length=80), nullable=False),
        sa.Column("object_uri", sa.String(length=2048), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("signature_ref", sa.String(length=2048), nullable=True),
        sa.Column("sbom_ref", sa.String(length=2048), nullable=True),
        sa.Column(
            "scan_status",
            sa.String(length=20),
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "content_hash ~ '^sha256:[a-f0-9]{64}$'", name="content_hash"
        ),
        sa.CheckConstraint("size_bytes >= 0", name="size_bytes"),
        sa.CheckConstraint(
            "scan_status IN ('PENDING', 'PASSED', 'FAILED')", name="scan_status"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(manifest_json) = 'object'", name="manifest_json"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_runtime_bundle__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "snapshot_id"],
            ["agent_snapshot.tenant_id", "agent_snapshot.id"],
            name="fk_runtime_bundle__tenant_snapshot__agent_snapshot",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_runtime_bundle"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_runtime_bundle__tenant_id_id"),
        sa.UniqueConstraint(
            "snapshot_id",
            "runtime_type",
            "compiler_version",
            "content_hash",
            name="uq_runtime_bundle__snapshot_runtime_compiler_hash",
        ),
    )
    op.create_index(
        "ix_runtime_bundle__tenant_snapshot",
        "runtime_bundle",
        ["tenant_id", "snapshot_id"],
        unique=False,
    )

    for table_name in ("release", "runtime_bundle"):
        op.execute(sa.text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"CREATE POLICY tenant_isolation ON {table_name} "
                f"USING ({TENANT_POLICY_EXPRESSION}) "
                f"WITH CHECK ({TENANT_POLICY_EXPRESSION})"
            )
        )


def downgrade() -> None:
    for table_name in ("runtime_bundle", "release"):
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table_name}"))
        op.execute(sa.text(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY"))
    op.drop_index("ix_runtime_bundle__tenant_snapshot", table_name="runtime_bundle")
    op.drop_table("runtime_bundle")
    op.drop_index("ix_release__tenant_status_created_at", table_name="release")
    op.drop_index("ix_release__tenant_agent_created_at", table_name="release")
    op.drop_table("release")

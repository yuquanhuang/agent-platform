"""Add Deployment activation history and fencing.

Revision ID: 0013_deployment_activation
Revises: 0012_release_bundle
Create Date: 2026-08-07
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_deployment_activation"
down_revision: str | None = "0012_release_bundle"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY_EXPRESSION = (
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)


def upgrade() -> None:
    op.add_column(
        "release",
        sa.Column(
            "activation_fencing_token",
            sa.BigInteger(),
            sa.Identity(),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_release__activation_fencing_token",
        "release",
        ["activation_fencing_token"],
    )
    op.create_unique_constraint(
        "uq_release__tenant_id_agent_snapshot",
        "release",
        ["tenant_id", "id", "agent_id", "snapshot_id"],
    )
    op.create_unique_constraint(
        "uq_runtime_bundle__tenant_snapshot_id",
        "runtime_bundle",
        ["tenant_id", "snapshot_id", "id"],
    )

    op.create_table(
        "deployment",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("release_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bundle_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("runtime_target_id", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'STAGED'"),
            nullable=False,
        ),
        sa.Column("compatibility_hash", sa.String(length=80), nullable=False),
        sa.Column("activation_fencing_token", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('STAGED', 'ACTIVE', 'DEGRADED', 'RETIRED', 'FAILED')",
            name="status",
        ),
        sa.CheckConstraint(
            "compatibility_hash ~ '^sha256:[a-f0-9]{64}$'",
            name="compatibility_hash",
        ),
        sa.CheckConstraint(
            "activation_fencing_token >= 1",
            name="fencing_token",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_deployment__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "release_id", "agent_id", "snapshot_id"],
            [
                "release.tenant_id",
                "release.id",
                "release.agent_id",
                "release.snapshot_id",
            ],
            name="fk_deployment__tenant_release_agent_snapshot__release",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agent_definition.tenant_id", "agent_definition.id"],
            name="fk_deployment__tenant_agent__agent_definition",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "snapshot_id"],
            ["agent_snapshot.tenant_id", "agent_snapshot.id"],
            name="fk_deployment__tenant_snapshot__agent_snapshot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "snapshot_id", "bundle_id"],
            [
                "runtime_bundle.tenant_id",
                "runtime_bundle.snapshot_id",
                "runtime_bundle.id",
            ],
            name="fk_deployment__tenant_snapshot_bundle__runtime_bundle",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deployment"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_deployment__tenant_id_id"),
        sa.UniqueConstraint(
            "release_id",
            "runtime_target_id",
            name="uq_deployment__release_runtime_target",
        ),
    )
    op.create_index(
        "uq_deployment__tenant_agent_runtime_target_active",
        "deployment",
        ["tenant_id", "agent_id", "runtime_target_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_index(
        "ix_deployment__tenant_agent_created_at",
        "deployment",
        ["tenant_id", "agent_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_deployment__tenant_runtime_target_status",
        "deployment",
        ["tenant_id", "runtime_target_id", "status"],
        unique=False,
    )
    op.execute(sa.text("ALTER TABLE deployment ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE deployment FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON deployment "
            f"USING ({TENANT_POLICY_EXPRESSION}) "
            f"WITH CHECK ({TENANT_POLICY_EXPRESSION})"
        )
    )
    op.create_foreign_key(
        "fk_agent_definition__tenant_active_deployment__deployment",
        "agent_definition",
        "deployment",
        ["tenant_id", "active_deployment_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_agent_definition__tenant_active_deployment__deployment",
        "agent_definition",
        type_="foreignkey",
    )
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON deployment"))
    op.execute(sa.text("ALTER TABLE deployment DISABLE ROW LEVEL SECURITY"))
    op.drop_index(
        "ix_deployment__tenant_runtime_target_status", table_name="deployment"
    )
    op.drop_index("ix_deployment__tenant_agent_created_at", table_name="deployment")
    op.drop_index(
        "uq_deployment__tenant_agent_runtime_target_active",
        table_name="deployment",
    )
    op.drop_table("deployment")
    op.drop_constraint(
        "uq_runtime_bundle__tenant_snapshot_id",
        "runtime_bundle",
        type_="unique",
    )
    op.drop_constraint(
        "uq_release__tenant_id_agent_snapshot", "release", type_="unique"
    )
    op.drop_constraint(
        "uq_release__activation_fencing_token", "release", type_="unique"
    )
    op.drop_column("release", "activation_fencing_token")

"""Add tenant-scoped versioned resource registry.

Revision ID: 0004_resource_registry
Revises: 0003_temporal_outbox
Create Date: 2026-08-06
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_resource_registry"
down_revision: str | None = "0003_temporal_outbox"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY_EXPRESSION = (
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        "resource_definition",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("code", postgresql.CITEXT(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'DRAFT'"),
            nullable=False,
        ),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "visibility",
            sa.String(length=20),
            server_default=sa.text("'private'"),
            nullable=False,
        ),
        sa.Column("current_draft_json", postgresql.JSONB(), nullable=False),
        sa.Column("draft_schema_version", sa.String(length=16), nullable=False),
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
            "resource_type IN ('prompt', 'skill', 'mcp', 'model_provider', "
            "'model_config', 'runtime_target', 'sandbox_profile')",
            name="ck_resource_definition__resource_type",
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'ACTIVE', 'DISABLED', 'DELETING', 'DELETED')",
            name="ck_resource_definition__status",
        ),
        sa.CheckConstraint(
            "visibility IN ('private', 'tenant')",
            name="ck_resource_definition__visibility",
        ),
        sa.CheckConstraint(
            "resource_version >= 1",
            name="ck_resource_definition__resource_version",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_resource_definition__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["app_user.id"],
            name="fk_resource_definition__owner_user_id__app_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name="fk_resource_definition__created_by__app_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["app_user.id"],
            name="fk_resource_definition__updated_by__app_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["deleted_by"],
            ["app_user.id"],
            name="fk_resource_definition__deleted_by__app_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_resource_definition"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_resource_definition__tenant_id_id"
        ),
    )
    op.create_index(
        "uq_resource_definition__tenant_type_code_active",
        "resource_definition",
        ["tenant_id", "resource_type", "code"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_resource_definition__tenant_type_status_created_at",
        "resource_definition",
        ["tenant_id", "resource_type", "status", "created_at"],
        unique=False,
    )

    op.create_table(
        "resource_version",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("definition_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_no", sa.BigInteger(), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("content_json", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(length=80), nullable=False),
        sa.Column("release_note", sa.String(length=2000), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'PUBLISHED'"),
            nullable=False,
        ),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("source_hash", sa.String(length=80), nullable=True),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint("version_no >= 1", name="ck_resource_version__version_no"),
        sa.CheckConstraint(
            "content_hash ~ '^sha256:[a-f0-9]{64}$'",
            name="ck_resource_version__content_hash",
        ),
        sa.CheckConstraint(
            "source_hash IS NULL OR source_hash ~ '^sha256:[a-f0-9]{64}$'",
            name="ck_resource_version__source_hash",
        ),
        sa.CheckConstraint(
            "status IN ('PUBLISHED', 'DISABLED')",
            name="ck_resource_version__status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_resource_version__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "definition_id"],
            ["resource_definition.tenant_id", "resource_definition.id"],
            name="fk_resource_version__tenant_definition__resource_definition",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["published_by"],
            ["app_user.id"],
            name="fk_resource_version__published_by__app_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_resource_version"),
        sa.UniqueConstraint(
            "definition_id",
            "version_no",
            name="uq_resource_version__definition_id_version_no",
        ),
        sa.UniqueConstraint(
            "definition_id",
            "content_hash",
            name="uq_resource_version__definition_id_content_hash",
        ),
    )
    op.create_index(
        "ix_resource_version__tenant_definition_published_at",
        "resource_version",
        ["tenant_id", "definition_id", "published_at"],
        unique=False,
    )

    for table_name in ("resource_definition", "resource_version"):
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
    for table_name in ("resource_version", "resource_definition"):
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table_name}"))
        op.execute(sa.text(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY"))
    op.drop_index(
        "ix_resource_version__tenant_definition_published_at",
        table_name="resource_version",
    )
    op.drop_table("resource_version")
    op.drop_index(
        "ix_resource_definition__tenant_type_status_created_at",
        table_name="resource_definition",
    )
    op.drop_index(
        "uq_resource_definition__tenant_type_code_active",
        table_name="resource_definition",
    )
    op.drop_table("resource_definition")

"""Create the tenant and IAM persistence foundation.

Revision ID: 0001_iam_tenant_foundation
Revises: None
Create Date: 2026-08-05
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_iam_tenant_foundation"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY_EXPRESSION = (
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)
TENANT_SCOPED_TABLES = ("tenant_member", "role", "role_binding")


def enable_tenant_rls(table_name: str) -> None:
    op.execute(sa.text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation ON {table_name} "
            f"USING ({TENANT_POLICY_EXPRESSION}) "
            f"WITH CHECK ({TENANT_POLICY_EXPRESSION})"
        )
    )


def disable_tenant_rls(table_name: str) -> None:
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table_name}"))
    op.execute(sa.text(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY"))


def upgrade() -> None:
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS citext"))

    op.create_table(
        "tenant",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("code", postgresql.CITEXT(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'ACTIVE'"),
            nullable=False,
        ),
        sa.Column("quota_policy_id", postgresql.UUID(as_uuid=True), nullable=True),
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "code ~ '^[a-z][a-z0-9_-]{2,63}$'", name="ck_tenant__code_format"
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED', 'DELETING', 'DELETED')",
            name="ck_tenant__status",
        ),
        sa.CheckConstraint("resource_version >= 1", name="ck_tenant__resource_version"),
        sa.PrimaryKeyConstraint("id", name="pk_tenant"),
        sa.UniqueConstraint("code", name="uq_tenant__code"),
    )
    op.create_index("ix_tenant__status", "tenant", ["status"], unique=False)

    op.create_table(
        "app_user",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("identity_issuer", sa.String(length=255), nullable=False),
        sa.Column("external_subject", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED', 'DELETED')",
            name="ck_app_user__status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_app_user"),
        sa.UniqueConstraint(
            "identity_issuer",
            "external_subject",
            name="uq_app_user__identity_issuer_external_subject",
        ),
    )
    op.create_index("ix_app_user__status", "app_user", ["status"], unique=False)

    op.create_table(
        "tenant_member",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'ACTIVE'"),
            nullable=False,
        ),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "resource_version",
            sa.BigInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "membership_version",
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED', 'DELETING', 'DELETED')",
            name="ck_tenant_member__status",
        ),
        sa.CheckConstraint(
            "resource_version >= 1", name="ck_tenant_member__resource_version"
        ),
        sa.CheckConstraint(
            "membership_version >= 1",
            name="ck_tenant_member__membership_version",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_tenant_member__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_tenant_member__user_id__app_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_member"),
        sa.UniqueConstraint(
            "tenant_id", "user_id", name="uq_tenant_member__tenant_id_user_id"
        ),
    )
    op.create_index(
        "ix_tenant_member__tenant_id_id",
        "tenant_member",
        ["tenant_id", "id"],
        unique=False,
    )
    op.create_index(
        "ix_tenant_member__tenant_id_status",
        "tenant_member",
        ["tenant_id", "status"],
        unique=False,
    )

    op.create_table(
        "role",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column(
            "scope",
            sa.String(length=32),
            server_default=sa.text("'TENANT'"),
            nullable=False,
        ),
        sa.Column(
            "built_in",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'ACTIVE'"),
            nullable=False,
        ),
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "code ~ '^[a-z][a-z0-9_-]{2,63}$'", name="ck_role__code_format"
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED', 'DELETING', 'DELETED')",
            name="ck_role__status",
        ),
        sa.CheckConstraint("resource_version >= 1", name="ck_role__resource_version"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_role__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_role"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_role__tenant_id_code"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_role__tenant_id_id"),
    )
    op.create_index(
        "ix_role__tenant_id_status",
        "role",
        ["tenant_id", "status"],
        unique=False,
    )

    op.create_table(
        "role_binding",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_type", sa.String(length=20), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=True),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "subject_type IN ('user', 'service')",
            name="ck_role_binding__subject_type",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_role_binding__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "role_id"],
            ["role.tenant_id", "role.id"],
            name="fk_role_binding__tenant_id_role_id__role",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_role_binding"),
    )
    op.create_index(
        "ix_role_binding__tenant_id_subject_type_subject_id",
        "role_binding",
        ["tenant_id", "subject_type", "subject_id"],
        unique=False,
    )
    op.create_index(
        "ix_role_binding__tenant_id_resource_type_resource_id",
        "role_binding",
        ["tenant_id", "resource_type", "resource_id"],
        unique=False,
    )
    op.create_index(
        "ix_role_binding__tenant_id_id",
        "role_binding",
        ["tenant_id", "id"],
        unique=False,
    )

    for table_name in TENANT_SCOPED_TABLES:
        enable_tenant_rls(table_name)


def downgrade() -> None:
    for table_name in reversed(TENANT_SCOPED_TABLES):
        disable_tenant_rls(table_name)

    op.drop_index("ix_role_binding__tenant_id_id", table_name="role_binding")
    op.drop_index(
        "ix_role_binding__tenant_id_resource_type_resource_id",
        table_name="role_binding",
    )
    op.drop_index(
        "ix_role_binding__tenant_id_subject_type_subject_id",
        table_name="role_binding",
    )
    op.drop_table("role_binding")
    op.drop_index("ix_role__tenant_id_status", table_name="role")
    op.drop_table("role")
    op.drop_index("ix_tenant_member__tenant_id_status", table_name="tenant_member")
    op.drop_index("ix_tenant_member__tenant_id_id", table_name="tenant_member")
    op.drop_table("tenant_member")
    op.drop_index("ix_app_user__status", table_name="app_user")
    op.drop_table("app_user")
    op.drop_index("ix_tenant__status", table_name="tenant")
    op.drop_table("tenant")

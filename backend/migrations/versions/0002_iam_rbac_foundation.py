"""Add basic role permissions and append-only audit facts.

Revision ID: 0002_iam_rbac_foundation
Revises: 0001_iam_tenant_foundation
Create Date: 2026-08-06
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_iam_rbac_foundation"
down_revision: str | None = "0001_iam_tenant_foundation"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY_EXPRESSION = (
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)


def upgrade() -> None:
    op.add_column(
        "tenant_member",
        sa.Column("display_name", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "tenant_member",
        sa.Column("email", postgresql.CITEXT(), nullable=True),
    )

    op.create_table(
        "role_permission",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column(
            "condition_schema_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("condition_json", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_role_permission__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "role_id"],
            ["role.tenant_id", "role.id"],
            name="fk_role_permission__tenant_id_role_id__role",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "role_id",
            "resource_type",
            "action",
            name="pk_role_permission",
        ),
    )
    op.create_index(
        "ix_role_permission__tenant_id_role_id",
        "role_permission",
        ["tenant_id", "role_id"],
        unique=False,
    )
    op.execute(sa.text("ALTER TABLE role_permission ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE role_permission FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON role_permission "
            f"USING ({TENANT_POLICY_EXPRESSION}) "
            f"WITH CHECK ({TENANT_POLICY_EXPRESSION})"
        )
    )

    op.create_table(
        "audit_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_type", sa.String(length=20), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("result", sa.String(length=20), nullable=False),
        sa.Column(
            "reason_codes",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("change_digest", sa.String(length=80), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column(
            "metadata_schema_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "actor_type IN ('user', 'service')",
            name="ck_audit_log__actor_type",
        ),
        sa.CheckConstraint(
            "result IN ('SUCCESS', 'DENIED', 'FAILED')",
            name="ck_audit_log__result",
        ),
        sa.CheckConstraint(
            "metadata_schema_version >= 1",
            name="ck_audit_log__metadata_schema_version",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_audit_log__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
    )
    op.create_index(
        "ix_audit_log__tenant_id_created_at",
        "audit_log",
        ["tenant_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_log__actor_type_actor_id",
        "audit_log",
        ["actor_type", "actor_id"],
        unique=False,
    )
    op.create_index(
        "ix_audit_log__resource_type_resource_id",
        "audit_log",
        ["resource_type", "resource_id"],
        unique=False,
    )
    op.create_index("ix_audit_log__action", "audit_log", ["action"], unique=False)

    op.create_table(
        "idempotency_record",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_type", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'IN_PROGRESS'"),
            nullable=False,
        ),
        sa.Column(
            "response_status",
            sa.Integer(),
            server_default=sa.text("200"),
            nullable=False,
        ),
        sa.Column("response_body_json", postgresql.JSONB(), nullable=True),
        sa.Column("response_etag", sa.String(length=128), nullable=True),
        sa.Column("response_ref", sa.String(length=255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
            "status IN ('IN_PROGRESS', 'COMPLETED')",
            name="ck_idempotency_record__status",
        ),
        sa.CheckConstraint(
            "response_status >= 100",
            name="ck_idempotency_record__response_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_idempotency_record__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_idempotency_record"),
    )
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_idempotency_record__scope "
            "ON idempotency_record "
            "(tenant_id, actor_id, operation_type, idempotency_key) "
            "NULLS NOT DISTINCT"
        )
    )
    op.create_index(
        "ix_idempotency_record__tenant_actor_operation",
        "idempotency_record",
        ["tenant_id", "actor_id", "operation_type", "idempotency_key"],
        unique=False,
    )

    op.create_table(
        "operation_record",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_type", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=True),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("result_json", postgresql.JSONB(), nullable=True),
        sa.Column("error_json", postgresql.JSONB(), nullable=True),
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
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('ACCEPTED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="ck_operation_record__status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_operation_record__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_operation_record"),
    )
    op.create_index(
        "ix_operation_record__tenant_id_created_at",
        "operation_record",
        ["tenant_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_operation_record__tenant_id_resource",
        "operation_record",
        ["tenant_id", "resource_type", "resource_id"],
        unique=False,
    )
    op.execute(sa.text("ALTER TABLE operation_record ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE operation_record FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON operation_record "
            f"USING ({TENANT_POLICY_EXPRESSION}) "
            f"WITH CHECK ({TENANT_POLICY_EXPRESSION})"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON operation_record"))
    op.execute(sa.text("ALTER TABLE operation_record DISABLE ROW LEVEL SECURITY"))
    op.drop_index(
        "ix_operation_record__tenant_id_resource", table_name="operation_record"
    )
    op.drop_index(
        "ix_operation_record__tenant_id_created_at", table_name="operation_record"
    )
    op.drop_table("operation_record")

    op.drop_index(
        "ix_idempotency_record__tenant_actor_operation", table_name="idempotency_record"
    )
    op.execute(sa.text("DROP INDEX IF EXISTS uq_idempotency_record__scope"))
    op.drop_table("idempotency_record")

    op.drop_index("ix_audit_log__action", table_name="audit_log")
    op.drop_index("ix_audit_log__resource_type_resource_id", table_name="audit_log")
    op.drop_index("ix_audit_log__actor_type_actor_id", table_name="audit_log")
    op.drop_index("ix_audit_log__tenant_id_created_at", table_name="audit_log")
    op.drop_table("audit_log")

    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON role_permission"))
    op.execute(sa.text("ALTER TABLE role_permission DISABLE ROW LEVEL SECURITY"))
    op.drop_index("ix_role_permission__tenant_id_role_id", table_name="role_permission")
    op.drop_table("role_permission")
    op.drop_column("tenant_member", "email")
    op.drop_column("tenant_member", "display_name")

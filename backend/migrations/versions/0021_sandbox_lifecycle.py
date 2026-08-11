"""Add tenant-isolated SandboxInstance and fenced Lease lifecycle.

Revision ID: 0021_sandbox_lifecycle
Revises: 0020_run_event_store
Create Date: 2026-08-09
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_sandbox_lifecycle"
down_revision: str | None = "0020_run_event_store"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY_EXPRESSION = (
    "tenant_id = nullif(current_setting('app.current_tenant_id', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        "sandbox_instance",
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
        sa.Column("execution_attempt", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("image_digest", sa.String(length=2048), nullable=False),
        sa.Column("policy_ref", sa.String(length=2048), nullable=False),
        sa.Column("policy_hash", sa.String(length=80), nullable=False),
        sa.Column("policy_schema_version", sa.String(length=32), nullable=False),
        sa.Column("policy_json", postgresql.JSONB(), nullable=False),
        sa.Column("bundle_ref", sa.String(length=2048), nullable=False),
        sa.Column("bundle_hash", sa.String(length=80), nullable=False),
        sa.Column("workspace_uri", sa.String(length=4096), nullable=False),
        sa.Column("runtime_target_id", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default=sa.text("'REQUESTED'"),
            nullable=False,
        ),
        sa.Column("provider_ref", sa.String(length=2048), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "provision_operation_id", postgresql.UUID(as_uuid=True), nullable=False
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
        sa.Column("terminated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=128), nullable=True),
        sa.CheckConstraint("scope IN ('run','session')", name="scope"),
        sa.CheckConstraint("execution_attempt >= 1", name="execution_attempt"),
        sa.CheckConstraint(
            "status IN ('REQUESTED','PROVISIONING','READY','IN_USE','FAILED',"
            "'QUARANTINED','TERMINATING','TERMINATED')",
            name="status",
        ),
        sa.CheckConstraint(
            "image_digest ~ '^[^@[:space:]]+@sha256:[a-f0-9]{64}$'",
            name="image_digest",
        ),
        sa.CheckConstraint("policy_hash ~ '^sha256:[a-f0-9]{64}$'", name="policy_hash"),
        sa.CheckConstraint("bundle_hash ~ '^sha256:[a-f0-9]{64}$'", name="bundle_hash"),
        sa.CheckConstraint("jsonb_typeof(policy_json) = 'object'", name="policy_json"),
        sa.CheckConstraint(
            "terminated_at IS NULL OR status = 'TERMINATED'",
            name="terminated_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_sandbox_instance__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "session_id"],
            ["agent_run.tenant_id", "agent_run.id", "agent_run.session_id"],
            name="fk_sandbox_instance__tenant_run_session__agent_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["tenant_member.tenant_id", "tenant_member.user_id"],
            name="fk_sandbox_instance__tenant_user__tenant_member",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provision_operation_id"],
            ["operation_record.id"],
            name="fk_sandbox_instance__provision_operation_id__operation_record",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sandbox_instance"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_sandbox_instance__tenant_id_id"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "execution_attempt",
            "policy_hash",
            name="uq_sandbox_instance__tenant_run_attempt_policy",
        ),
        sa.UniqueConstraint(
            "provision_operation_id",
            name="uq_sandbox_instance__provision_operation_id",
        ),
    )
    op.create_index(
        "ix_sandbox_instance__tenant_run_attempt",
        "sandbox_instance",
        ["tenant_id", "run_id", "execution_attempt"],
    )
    op.create_index(
        "ix_sandbox_instance__tenant_status_updated_at",
        "sandbox_instance",
        ["tenant_id", "status", "updated_at"],
    )

    op.create_table(
        "sandbox_lease",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sandbox_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("holder_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_attempt", sa.Integer(), nullable=False),
        sa.Column("fencing_token_hash", sa.String(length=80), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("execution_attempt >= 1", name="execution_attempt"),
        sa.CheckConstraint(
            "fencing_token_hash ~ '^sha256:[a-f0-9]{64}$'",
            name="fencing_token_hash",
        ),
        sa.CheckConstraint("expires_at > acquired_at", name="expires_at"),
        sa.CheckConstraint(
            "released_at IS NULL OR released_at >= acquired_at", name="released_at"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_sandbox_lease__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "sandbox_id"],
            ["sandbox_instance.tenant_id", "sandbox_instance.id"],
            name="fk_sandbox_lease__tenant_sandbox__sandbox_instance",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "holder_run_id"],
            ["agent_run.tenant_id", "agent_run.id"],
            name="fk_sandbox_lease__tenant_run__agent_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sandbox_lease"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_sandbox_lease__tenant_id_id"),
    )
    op.create_index(
        "uq_sandbox_lease__sandbox_active",
        "sandbox_lease",
        ["sandbox_id"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.create_index(
        "ix_sandbox_lease__tenant_run_expires_at",
        "sandbox_lease",
        ["tenant_id", "holder_run_id", "expires_at"],
    )

    for table_name in ("sandbox_instance", "sandbox_lease"):
        op.execute(sa.text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"CREATE POLICY tenant_isolation ON {table_name} "
                f"USING ({TENANT_POLICY_EXPRESSION}) "
                f"WITH CHECK ({TENANT_POLICY_EXPRESSION})"
            )
        )

    op.execute(sa.text(_SANDBOX_INSTANCE_GUARD_FUNCTION))
    op.execute(sa.text(_SANDBOX_LEASE_GUARD_FUNCTION))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_sandbox_instance__guard BEFORE UPDATE OR DELETE "
            "ON sandbox_instance FOR EACH ROW EXECUTE FUNCTION guard_sandbox_instance()"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_sandbox_lease__guard BEFORE UPDATE OR DELETE "
            "ON sandbox_lease FOR EACH ROW EXECUTE FUNCTION guard_sandbox_lease()"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER trg_sandbox_lease__guard ON sandbox_lease"))
    op.execute(sa.text("DROP TRIGGER trg_sandbox_instance__guard ON sandbox_instance"))
    op.execute(sa.text("DROP FUNCTION guard_sandbox_lease()"))
    op.execute(sa.text("DROP FUNCTION guard_sandbox_instance()"))
    for table_name in ("sandbox_lease", "sandbox_instance"):
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table_name}"))
        op.execute(sa.text(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY"))
    op.drop_index("ix_sandbox_lease__tenant_run_expires_at", table_name="sandbox_lease")
    op.drop_index("uq_sandbox_lease__sandbox_active", table_name="sandbox_lease")
    op.drop_table("sandbox_lease")
    op.drop_index(
        "ix_sandbox_instance__tenant_status_updated_at",
        table_name="sandbox_instance",
    )
    op.drop_index(
        "ix_sandbox_instance__tenant_run_attempt", table_name="sandbox_instance"
    )
    op.drop_table("sandbox_instance")


_SANDBOX_INSTANCE_GUARD_FUNCTION = """
CREATE FUNCTION guard_sandbox_instance() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'sandbox_instance cannot be deleted';
    END IF;
    IF ROW(NEW.id, NEW.tenant_id, NEW.user_id, NEW.session_id, NEW.run_id,
           NEW.execution_attempt, NEW.scope, NEW.image_digest, NEW.policy_ref,
           NEW.policy_hash, NEW.policy_schema_version, NEW.policy_json,
           NEW.bundle_ref, NEW.bundle_hash, NEW.workspace_uri,
           NEW.runtime_target_id, NEW.provision_operation_id, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.tenant_id, OLD.user_id, OLD.session_id, OLD.run_id,
           OLD.execution_attempt, OLD.scope, OLD.image_digest, OLD.policy_ref,
           OLD.policy_hash, OLD.policy_schema_version, OLD.policy_json,
           OLD.bundle_ref, OLD.bundle_hash, OLD.workspace_uri,
           OLD.runtime_target_id, OLD.provision_operation_id, OLD.created_at) THEN
        RAISE EXCEPTION 'sandbox_instance immutable identity cannot change';
    END IF;
    IF OLD.provider_ref IS NOT NULL AND NEW.provider_ref IS DISTINCT FROM OLD.provider_ref THEN
        RAISE EXCEPTION 'sandbox_instance provider_ref cannot change';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'REQUESTED' AND NEW.status IN ('PROVISIONING', 'FAILED', 'TERMINATING')) OR
        (OLD.status = 'PROVISIONING' AND NEW.status IN ('READY', 'FAILED', 'QUARANTINED', 'TERMINATING')) OR
        (OLD.status = 'READY' AND NEW.status IN ('IN_USE', 'QUARANTINED', 'TERMINATING')) OR
        (OLD.status = 'IN_USE' AND NEW.status IN ('READY', 'QUARANTINED', 'TERMINATING')) OR
        (OLD.status = 'FAILED' AND NEW.status IN ('TERMINATING', 'TERMINATED')) OR
        (OLD.status = 'QUARANTINED' AND NEW.status = 'TERMINATING') OR
        (OLD.status = 'TERMINATING' AND NEW.status IN ('TERMINATED', 'QUARANTINED'))
    ) THEN
        RAISE EXCEPTION 'sandbox_instance lifecycle transition is not allowed';
    END IF;
    IF OLD.terminated_at IS NOT NULL AND NEW.terminated_at IS DISTINCT FROM OLD.terminated_at THEN
        RAISE EXCEPTION 'sandbox_instance terminated_at cannot change';
    END IF;
    RETURN NEW;
END; $$
"""

_SANDBOX_LEASE_GUARD_FUNCTION = """
CREATE FUNCTION guard_sandbox_lease() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'sandbox_lease cannot be deleted';
    END IF;
    IF ROW(NEW.id, NEW.tenant_id, NEW.sandbox_id, NEW.holder_run_id,
           NEW.execution_attempt, NEW.fencing_token_hash, NEW.acquired_at,
           NEW.expires_at)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.tenant_id, OLD.sandbox_id, OLD.holder_run_id,
           OLD.execution_attempt, OLD.fencing_token_hash, OLD.acquired_at,
           OLD.expires_at) THEN
        RAISE EXCEPTION 'sandbox_lease immutable fields cannot change';
    END IF;
    IF OLD.released_at IS NOT NULL AND NEW.released_at IS DISTINCT FROM OLD.released_at THEN
        RAISE EXCEPTION 'sandbox_lease cannot be reopened or released twice';
    END IF;
    RETURN NEW;
END; $$
"""

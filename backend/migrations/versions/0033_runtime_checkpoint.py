"""Add durable AgentScope checkpoints and bind them to Approval facts.

Revision ID: 0033_runtime_checkpoint
Revises: 0032_artifact_download_gateway
Create Date: 2026-08-11
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033_runtime_checkpoint"
down_revision: str | None = "0032_artifact_download_gateway"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY = (
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)


def upgrade() -> None:
    op.add_column(
        "approval_request",
        sa.Column("runtime_checkpoint_ref", sa.String(length=2048), nullable=True),
    )
    op.create_check_constraint(
        "runtime_checkpoint_ref",
        "approval_request",
        "runtime_checkpoint_ref IS NULL OR "
        "runtime_checkpoint_ref ~ '^state://tenant/[A-Za-z0-9._:/-]+$'",
    )
    op.execute(sa.text(_APPROVAL_REQUEST_GUARD_WITH_CHECKPOINT))

    op.create_table(
        "runtime_checkpoint",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_attempt", sa.Integer(), nullable=False),
        sa.Column("sequence_no", sa.BigInteger(), nullable=False),
        sa.Column("state_ref", sa.String(length=2048), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("content_hash", sa.String(length=80), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("fencing_token_hash", sa.String(length=80), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("execution_attempt >= 1", name="execution_attempt"),
        sa.CheckConstraint("sequence_no >= 1", name="sequence_no"),
        sa.CheckConstraint("size_bytes >= 1", name="size_bytes"),
        sa.CheckConstraint(
            "content_hash ~ '^sha256:[a-f0-9]{64}$'", name="content_hash"
        ),
        sa.CheckConstraint(
            "fencing_token_hash ~ '^sha256:[a-f0-9]{64}$'",
            name="fencing_token_hash",
        ),
        sa.CheckConstraint("status IN ('PENDING','AVAILABLE','FAILED')", name="status"),
        sa.CheckConstraint("expires_at > created_at", name="expires_at"),
        sa.CheckConstraint(
            "(status = 'AVAILABLE' AND available_at IS NOT NULL) OR "
            "(status <> 'AVAILABLE' AND available_at IS NULL)",
            name="available_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_runtime_checkpoint__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["agent_run.tenant_id", "agent_run.id"],
            name="fk_runtime_checkpoint__tenant_run__agent_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_runtime_checkpoint"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_runtime_checkpoint__tenant_id_id"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "execution_attempt",
            "sequence_no",
            name="uq_runtime_checkpoint__tenant_run_attempt_sequence",
        ),
        sa.UniqueConstraint(
            "tenant_id", "state_ref", name="uq_runtime_checkpoint__tenant_state_ref"
        ),
    )
    op.create_index(
        "ix_runtime_checkpoint__tenant_run_attempt_latest",
        "runtime_checkpoint",
        ["tenant_id", "run_id", "execution_attempt", sa.text("sequence_no DESC")],
        postgresql_where=sa.text("status = 'AVAILABLE'"),
    )
    op.execute(sa.text("ALTER TABLE runtime_checkpoint ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE runtime_checkpoint FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON runtime_checkpoint "
            f"USING ({TENANT_POLICY}) WITH CHECK ({TENANT_POLICY})"
        )
    )
    op.execute(sa.text(_RUNTIME_CHECKPOINT_GUARD))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_runtime_checkpoint__guard "
            "BEFORE UPDATE OR DELETE ON runtime_checkpoint "
            "FOR EACH ROW EXECUTE FUNCTION guard_runtime_checkpoint()"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("DROP TRIGGER trg_runtime_checkpoint__guard ON runtime_checkpoint")
    )
    op.execute(sa.text("DROP FUNCTION guard_runtime_checkpoint()"))
    op.execute(sa.text("DROP POLICY tenant_isolation ON runtime_checkpoint"))
    op.execute(sa.text("ALTER TABLE runtime_checkpoint DISABLE ROW LEVEL SECURITY"))
    op.drop_index(
        "ix_runtime_checkpoint__tenant_run_attempt_latest",
        table_name="runtime_checkpoint",
    )
    op.drop_table("runtime_checkpoint")
    op.execute(sa.text(_APPROVAL_REQUEST_GUARD_WITHOUT_CHECKPOINT))
    op.drop_constraint(
        op.f("ck_approval_request__runtime_checkpoint_ref"),
        "approval_request",
        type_="check",
    )
    op.drop_column("approval_request", "runtime_checkpoint_ref")


_RUNTIME_CHECKPOINT_GUARD = """
CREATE FUNCTION guard_runtime_checkpoint() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'runtime checkpoints are immutable facts';
    END IF;
    IF ROW(NEW.tenant_id, NEW.run_id, NEW.execution_attempt, NEW.sequence_no,
           NEW.state_ref, NEW.object_key, NEW.content_hash, NEW.size_bytes,
           NEW.fencing_token_hash, NEW.created_at, NEW.expires_at)
       IS DISTINCT FROM
       ROW(OLD.tenant_id, OLD.run_id, OLD.execution_attempt, OLD.sequence_no,
           OLD.state_ref, OLD.object_key, OLD.content_hash, OLD.size_bytes,
           OLD.fencing_token_hash, OLD.created_at, OLD.expires_at) THEN
        RAISE EXCEPTION 'runtime checkpoint identity and content are immutable';
    END IF;
    IF OLD.status = 'PENDING' AND NEW.status = 'AVAILABLE' THEN
        IF NEW.available_at IS NULL THEN
            RAISE EXCEPTION 'available checkpoint requires available_at';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status = 'PENDING' AND NEW.status = 'FAILED' THEN
        IF NEW.available_at IS NOT NULL THEN
            RAISE EXCEPTION 'failed checkpoint cannot have available_at';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status = NEW.status
       AND OLD.available_at IS NOT DISTINCT FROM NEW.available_at THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'invalid runtime checkpoint transition % -> %',
        OLD.status, NEW.status;
END;
$$;
"""


_APPROVAL_REQUEST_GUARD_WITH_CHECKPOINT = """
CREATE OR REPLACE FUNCTION guard_approval_request() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'approval requests are immutable facts';
    END IF;
    IF ROW(NEW.tenant_id, NEW.run_id, NEW.execution_attempt, NEW.requester_id,
           NEW.tool_call_id, NEW.tool_name, NEW.tool_schema_hash,
           NEW.parameter_digest, NEW.policy_version, NEW.deployment_id,
           NEW.expires_at, NEW.self_approval_allowed, NEW.runtime_checkpoint_ref,
           NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.tenant_id, OLD.run_id, OLD.execution_attempt, OLD.requester_id,
           OLD.tool_call_id, OLD.tool_name, OLD.tool_schema_hash,
           OLD.parameter_digest, OLD.policy_version, OLD.deployment_id,
           OLD.expires_at, OLD.self_approval_allowed, OLD.runtime_checkpoint_ref,
           OLD.created_at) THEN
        RAISE EXCEPTION 'approval request bindings are immutable';
    END IF;
    IF OLD.status = NEW.status THEN
        IF OLD.workflow_signal_sent_at IS NOT NULL
           OR NEW.workflow_signal_sent_at IS NULL
           OR NEW.status NOT IN
              ('APPROVED','REJECTED','EXPIRED','CANCELLED','CONSUMED')
           OR NEW.resource_version <> OLD.resource_version
           OR NEW.updated_at <> OLD.updated_at THEN
            RAISE EXCEPTION 'invalid approval workflow signal delivery update';
        END IF;
        RETURN NEW;
    END IF;
    IF NOT (
        (OLD.status = 'PENDING' AND NEW.status IN
            ('APPROVED','REJECTED','EXPIRED','CANCELLED')) OR
        (OLD.status = 'APPROVED' AND NEW.status IN ('EXPIRED','CONSUMED'))
    ) THEN
        RAISE EXCEPTION 'invalid approval request transition % -> %',
            OLD.status, NEW.status;
    END IF;
    IF NEW.resource_version <> OLD.resource_version + 1 THEN
        RAISE EXCEPTION 'approval request resource_version must increment once';
    END IF;
    IF NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'approval request updated_at cannot move backwards';
    END IF;
    RETURN NEW;
END;
$$;
"""


_APPROVAL_REQUEST_GUARD_WITHOUT_CHECKPOINT = (
    _APPROVAL_REQUEST_GUARD_WITH_CHECKPOINT.replace(
        ", NEW.runtime_checkpoint_ref", ""
    ).replace(", OLD.runtime_checkpoint_ref", "")
)

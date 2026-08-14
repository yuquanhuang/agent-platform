"""Add durable bounded Run admission queue facts.

Revision ID: 0038_run_admission_queue
Revises: 0037_artifact_upload_reclamation
Create Date: 2026-08-12
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0038_run_admission_queue"
down_revision: str | None = "0037_artifact_upload_reclamation"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY = (
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        "run_admission_queue",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "priority",
            sa.String(length=16),
            server_default=sa.text("'NORMAL'"),
            nullable=False,
        ),
        sa.Column("capacity_domain", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'WAITING'"),
            nullable=False,
        ),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "quota_policy_version_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("capacity_snapshot_json", postgresql.JSONB(), nullable=True),
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
        sa.CheckConstraint("priority IN ('NORMAL','HIGH')", name="priority"),
        sa.CheckConstraint(
            "status IN ('WAITING','ADMITTED','CANCELLED','TIMED_OUT')",
            name="status",
        ),
        sa.CheckConstraint(
            "length(capacity_domain) BETWEEN 1 AND 128", name="capacity_domain"
        ),
        sa.CheckConstraint("deadline_at > queued_at", name="deadline_after_queue"),
        sa.CheckConstraint("resource_version >= 1", name="resource_version"),
        sa.CheckConstraint(
            "(status = 'WAITING' AND admitted_at IS NULL AND cancelled_at IS NULL) OR "
            "(status = 'ADMITTED' AND admitted_at IS NOT NULL AND cancelled_at IS NULL) OR "
            "(status IN ('CANCELLED','TIMED_OUT') AND admitted_at IS NULL "
            "AND cancelled_at IS NOT NULL)",
            name="terminal_timestamps",
        ),
        sa.CheckConstraint(
            "capacity_snapshot_json IS NULL OR "
            "jsonb_typeof(capacity_snapshot_json) = 'object'",
            name="capacity_snapshot_json",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_run_admission_queue__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["agent_run.tenant_id", "agent_run.id"],
            name="fk_run_admission_queue__tenant_run__agent_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_run_admission_queue"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_run_admission_queue__tenant_id_id"
        ),
        sa.UniqueConstraint(
            "tenant_id", "run_id", name="uq_run_admission_queue__tenant_run"
        ),
    )
    op.create_index(
        "ix_run_admission_queue__tenant_status_priority_queued",
        "run_admission_queue",
        ["tenant_id", "status", "priority", "queued_at", "id"],
    )
    op.create_index(
        "ix_run_admission_queue__tenant_status_deadline",
        "run_admission_queue",
        ["tenant_id", "status", "deadline_at", "id"],
    )
    op.create_index(
        "ix_run_admission_queue__domain_status_queued",
        "run_admission_queue",
        ["capacity_domain", "status", "queued_at", "id"],
    )
    op.execute(sa.text("ALTER TABLE run_admission_queue ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE run_admission_queue FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON run_admission_queue "
            f"USING ({TENANT_POLICY}) WITH CHECK ({TENANT_POLICY})"
        )
    )
    op.execute(sa.text(_QUEUE_GUARD_FUNCTION))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_run_admission_queue__guard "
            "BEFORE UPDATE OR DELETE ON run_admission_queue FOR EACH ROW "
            "EXECUTE FUNCTION guard_run_admission_queue_mutation()"
        )
    )
    op.execute(sa.text("DROP TRIGGER trg_agent_run__guard ON agent_run"))
    op.execute(sa.text("DROP FUNCTION guard_agent_run_mutation()"))
    op.execute(sa.text(_RUN_GUARD_FUNCTION))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_agent_run__guard "
            "BEFORE UPDATE OR DELETE ON agent_run FOR EACH ROW "
            "EXECUTE FUNCTION guard_agent_run_mutation()"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER trg_agent_run__guard ON agent_run"))
    op.execute(sa.text("DROP FUNCTION guard_agent_run_mutation()"))
    op.execute(sa.text(_RUN_GUARD_FUNCTION_DOWNGRADE))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_agent_run__guard "
            "BEFORE UPDATE OR DELETE ON agent_run FOR EACH ROW "
            "EXECUTE FUNCTION guard_agent_run_mutation()"
        )
    )
    op.execute(
        sa.text("DROP TRIGGER trg_run_admission_queue__guard ON run_admission_queue")
    )
    op.execute(sa.text("DROP FUNCTION guard_run_admission_queue_mutation()"))
    op.execute(sa.text("DROP POLICY tenant_isolation ON run_admission_queue"))
    op.execute(sa.text("ALTER TABLE run_admission_queue DISABLE ROW LEVEL SECURITY"))
    op.drop_index(
        "ix_run_admission_queue__domain_status_queued",
        table_name="run_admission_queue",
    )
    op.drop_index(
        "ix_run_admission_queue__tenant_status_deadline",
        table_name="run_admission_queue",
    )
    op.drop_index(
        "ix_run_admission_queue__tenant_status_priority_queued",
        table_name="run_admission_queue",
    )
    op.drop_table("run_admission_queue")


_QUEUE_GUARD_FUNCTION = """
CREATE FUNCTION guard_run_admission_queue_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'run admission queue facts cannot be deleted';
    END IF;
    IF ROW(NEW.tenant_id, NEW.run_id, NEW.priority, NEW.capacity_domain,
           NEW.queued_at, NEW.deadline_at, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.tenant_id, OLD.run_id, OLD.priority, OLD.capacity_domain,
           OLD.queued_at, OLD.deadline_at, OLD.created_at) THEN
        RAISE EXCEPTION 'run admission queue binding facts are immutable';
    END IF;
    IF NEW.resource_version <> OLD.resource_version + 1 THEN
        RAISE EXCEPTION 'run admission queue resource_version must increment by one';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        OLD.status = 'WAITING' AND NEW.status IN ('ADMITTED','CANCELLED','TIMED_OUT')
    ) THEN
        RAISE EXCEPTION 'invalid run admission queue transition: % -> %', OLD.status, NEW.status;
    END IF;
    RETURN NEW;
END $$;
"""


_RUN_GUARD_FUNCTION = """
CREATE FUNCTION guard_agent_run_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE linked_role text; linked_source uuid;
BEGIN
    IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'agent_run facts cannot be deleted'; END IF;
    IF TG_OP = 'UPDATE' THEN
        IF ROW(NEW.tenant_id, NEW.session_id, NEW.branch_id, NEW.user_message_id,
               NEW.agent_id, NEW.snapshot_id, NEW.deployment_id,
               NEW.idempotency_key, NEW.client_request_id, NEW.retry_of_run_id,
               NEW.timeout_seconds, NEW.token_budget, NEW.cost_budget_amount,
               NEW.cost_budget_currency, NEW.created_by, NEW.created_at)
           IS DISTINCT FROM
           ROW(OLD.tenant_id, OLD.session_id, OLD.branch_id, OLD.user_message_id,
               OLD.agent_id, OLD.snapshot_id, OLD.deployment_id,
               OLD.idempotency_key, OLD.client_request_id, OLD.retry_of_run_id,
               OLD.timeout_seconds, OLD.token_budget, OLD.cost_budget_amount,
               OLD.cost_budget_currency, OLD.created_by, OLD.created_at) THEN
            RAISE EXCEPTION 'agent_run execution inputs are immutable';
        END IF;
        IF OLD.assistant_message_id IS NOT NULL
           AND NEW.assistant_message_id IS DISTINCT FROM OLD.assistant_message_id THEN
            RAISE EXCEPTION 'agent_run assistant message binding is immutable';
        END IF;
        IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
            (OLD.status = 'CREATED' AND NEW.status IN ('QUEUED','CANCELLING','FAILED')) OR
            (OLD.status = 'QUEUED' AND NEW.status IN ('PREPARING','CANCELLED','CANCELLING','TIMEOUT')) OR
            (OLD.status = 'PREPARING' AND NEW.status IN ('RUNNING','CANCELLING','FAILED','TIMEOUT')) OR
            (OLD.status = 'RUNNING' AND NEW.status IN ('WAITING_APPROVAL','CANCELLING','SUCCEEDED','FAILED','TIMEOUT')) OR
            (OLD.status = 'WAITING_APPROVAL' AND NEW.status IN ('RUNNING','CANCELLING','TIMEOUT')) OR
            (OLD.status = 'CANCELLING' AND NEW.status IN ('CANCELLED','FAILED'))
        ) THEN
            RAISE EXCEPTION 'invalid agent_run state transition: % -> %', OLD.status, NEW.status;
        END IF;
    END IF;
    SELECT role, source_run_id INTO linked_role, linked_source
      FROM chat_message WHERE tenant_id = NEW.tenant_id
       AND id = NEW.user_message_id AND session_id = NEW.session_id;
    IF linked_role IS DISTINCT FROM 'USER' OR linked_source IS NOT NULL THEN
        RAISE EXCEPTION 'agent_run user message link is invalid';
    END IF;
    IF NEW.assistant_message_id IS NOT NULL THEN
        SELECT role, source_run_id INTO linked_role, linked_source
          FROM chat_message WHERE tenant_id = NEW.tenant_id
           AND id = NEW.assistant_message_id AND session_id = NEW.session_id;
        IF linked_role IS DISTINCT FROM 'ASSISTANT' OR linked_source IS DISTINCT FROM NEW.id THEN
            RAISE EXCEPTION 'agent_run assistant message link is invalid';
        END IF;
    END IF;
    RETURN NEW;
END $$;
"""

_RUN_GUARD_FUNCTION_DOWNGRADE = _RUN_GUARD_FUNCTION.replace(
    "'PREPARING','CANCELLED','CANCELLING','TIMEOUT'",
    "'PREPARING','CANCELLING','TIMEOUT'",
)

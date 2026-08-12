"""Track durable delivery of Approval decisions to Temporal.

Revision ID: 0030_approval_signal_reconcile
Revises: 0029_audit_query_retention
Create Date: 2026-08-11
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0030_approval_signal_reconcile"
down_revision: str | None = "0029_audit_query_retention"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "approval_request",
        sa.Column("workflow_signal_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "workflow_signal_terminal",
        "approval_request",
        "workflow_signal_sent_at IS NULL OR status IN "
        "('APPROVED','REJECTED','EXPIRED','CANCELLED','CONSUMED')",
    )
    op.create_index(
        "ix_approval_request__tenant_unsent_signal",
        "approval_request",
        ["tenant_id", "status", "updated_at"],
        postgresql_where=sa.text("workflow_signal_sent_at IS NULL"),
    )
    op.execute(sa.text("DROP TRIGGER trg_approval_request__guard ON approval_request"))
    op.execute(sa.text("DROP FUNCTION guard_approval_request()"))
    op.execute(sa.text(_APPROVAL_REQUEST_GUARD_FUNCTION))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_approval_request__guard "
            "BEFORE UPDATE OR DELETE ON approval_request "
            "FOR EACH ROW EXECUTE FUNCTION guard_approval_request()"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER trg_approval_request__guard ON approval_request"))
    op.execute(sa.text("DROP FUNCTION guard_approval_request()"))
    op.drop_index(
        "ix_approval_request__tenant_unsent_signal",
        table_name="approval_request",
    )
    op.drop_constraint(
        op.f("ck_approval_request__workflow_signal_terminal"),
        "approval_request",
        type_="check",
    )
    op.drop_column("approval_request", "workflow_signal_sent_at")
    op.execute(sa.text(_PREVIOUS_APPROVAL_REQUEST_GUARD_FUNCTION))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_approval_request__guard "
            "BEFORE UPDATE OR DELETE ON approval_request "
            "FOR EACH ROW EXECUTE FUNCTION guard_approval_request()"
        )
    )


_APPROVAL_REQUEST_GUARD_FUNCTION = """
CREATE FUNCTION guard_approval_request() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'approval requests are immutable facts';
    END IF;
    IF ROW(NEW.tenant_id, NEW.run_id, NEW.execution_attempt, NEW.requester_id,
           NEW.tool_call_id, NEW.tool_name, NEW.tool_schema_hash,
           NEW.parameter_digest, NEW.policy_version, NEW.deployment_id,
           NEW.expires_at, NEW.self_approval_allowed, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.tenant_id, OLD.run_id, OLD.execution_attempt, OLD.requester_id,
           OLD.tool_call_id, OLD.tool_name, OLD.tool_schema_hash,
           OLD.parameter_digest, OLD.policy_version, OLD.deployment_id,
           OLD.expires_at, OLD.self_approval_allowed, OLD.created_at) THEN
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


_PREVIOUS_APPROVAL_REQUEST_GUARD_FUNCTION = """
CREATE FUNCTION guard_approval_request() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'approval requests are immutable facts';
    END IF;
    IF ROW(NEW.tenant_id, NEW.run_id, NEW.execution_attempt, NEW.requester_id,
           NEW.tool_call_id, NEW.tool_name, NEW.tool_schema_hash,
           NEW.parameter_digest, NEW.policy_version, NEW.deployment_id,
           NEW.expires_at, NEW.self_approval_allowed, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.tenant_id, OLD.run_id, OLD.execution_attempt, OLD.requester_id,
           OLD.tool_call_id, OLD.tool_name, OLD.tool_schema_hash,
           OLD.parameter_digest, OLD.policy_version, OLD.deployment_id,
           OLD.expires_at, OLD.self_approval_allowed, OLD.created_at) THEN
        RAISE EXCEPTION 'approval request bindings are immutable';
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

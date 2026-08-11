"""Add one-time Execution Tickets consumed by the internal Tool Gateway.

Revision ID: 0028_execution_ticket_gateway
Revises: 0027_approval_control_plane
Create Date: 2026-08-10
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028_execution_ticket_gateway"
down_revision: str | None = "0027_approval_control_plane"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY_EXPRESSION = (
    "tenant_id = nullif(current_setting('app.current_tenant_id', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        "execution_ticket",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_attempt", sa.Integer(), nullable=False),
        sa.Column("requester_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tool_name", sa.String(length=255), nullable=False),
        sa.Column("tool_schema_hash", sa.String(length=80), nullable=False),
        sa.Column("parameter_digest", sa.String(length=80), nullable=False),
        sa.Column("policy_version", sa.String(length=128), nullable=False),
        sa.Column("deployment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("nonce_hash", sa.String(length=80), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "single_use", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("execution_attempt >= 1", name="execution_attempt"),
        sa.CheckConstraint("length(tool_name) >= 1", name="tool_name"),
        sa.CheckConstraint(
            "tool_schema_hash ~ '^sha256:[a-f0-9]{64}$'", name="tool_schema_hash"
        ),
        sa.CheckConstraint(
            "parameter_digest ~ '^sha256:[a-f0-9]{64}$'", name="parameter_digest"
        ),
        sa.CheckConstraint("length(policy_version) >= 1", name="policy_version"),
        sa.CheckConstraint("nonce_hash ~ '^sha256:[a-f0-9]{64}$'", name="nonce_hash"),
        sa.CheckConstraint("single_use IS TRUE", name="single_use"),
        sa.CheckConstraint("expires_at > created_at", name="expires_at"),
        sa.CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at", name="consumed_at"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_execution_ticket__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "approval_id"],
            ["approval_request.tenant_id", "approval_request.id"],
            name="fk_execution_ticket__tenant_approval__approval_request",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["agent_run.tenant_id", "agent_run.id"],
            name="fk_execution_ticket__tenant_run__agent_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "requester_id"],
            ["tenant_member.tenant_id", "tenant_member.user_id"],
            name="fk_execution_ticket__tenant_requester__tenant_member",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "deployment_id"],
            ["deployment.tenant_id", "deployment.id"],
            name="fk_execution_ticket__tenant_deployment__deployment",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_execution_ticket"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_execution_ticket__tenant_id_id"
        ),
        sa.UniqueConstraint("approval_id", name="uq_execution_ticket__approval_id"),
    )
    op.create_index(
        "ix_execution_ticket__tenant_run_expires_at",
        "execution_ticket",
        ["tenant_id", "run_id", "expires_at"],
    )
    op.create_index(
        "ix_execution_ticket__tenant_approval",
        "execution_ticket",
        ["tenant_id", "approval_id"],
    )
    op.execute(sa.text("ALTER TABLE execution_ticket ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE execution_ticket FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON execution_ticket "
            f"USING ({TENANT_POLICY_EXPRESSION}) WITH CHECK ({TENANT_POLICY_EXPRESSION})"
        )
    )
    op.execute(sa.text(_EXECUTION_TICKET_GUARD_FUNCTION))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_execution_ticket__guard "
            "BEFORE UPDATE OR DELETE ON execution_ticket FOR EACH ROW "
            "EXECUTE FUNCTION guard_execution_ticket()"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER trg_execution_ticket__guard ON execution_ticket"))
    op.execute(sa.text("DROP FUNCTION guard_execution_ticket()"))
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON execution_ticket"))
    op.execute(sa.text("ALTER TABLE execution_ticket DISABLE ROW LEVEL SECURITY"))
    op.drop_index("ix_execution_ticket__tenant_approval", table_name="execution_ticket")
    op.drop_index(
        "ix_execution_ticket__tenant_run_expires_at", table_name="execution_ticket"
    )
    op.drop_table("execution_ticket")


_EXECUTION_TICKET_GUARD_FUNCTION = """
CREATE FUNCTION guard_execution_ticket() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'execution tickets are immutable facts';
    END IF;
    IF ROW(NEW.id, NEW.tenant_id, NEW.approval_id, NEW.run_id,
           NEW.execution_attempt, NEW.requester_id, NEW.tool_name,
           NEW.tool_schema_hash, NEW.parameter_digest, NEW.policy_version,
           NEW.deployment_id, NEW.nonce_hash, NEW.expires_at, NEW.single_use,
           NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.tenant_id, OLD.approval_id, OLD.run_id,
           OLD.execution_attempt, OLD.requester_id, OLD.tool_name,
           OLD.tool_schema_hash, OLD.parameter_digest, OLD.policy_version,
           OLD.deployment_id, OLD.nonce_hash, OLD.expires_at, OLD.single_use,
           OLD.created_at) THEN
        RAISE EXCEPTION 'execution ticket bindings are immutable';
    END IF;
    IF OLD.consumed_at IS NOT NULL OR NEW.consumed_at IS NULL THEN
        RAISE EXCEPTION 'execution ticket can only be consumed once';
    END IF;
    IF NEW.consumed_at < OLD.created_at THEN
        RAISE EXCEPTION 'execution ticket consumed_at is invalid';
    END IF;
    RETURN NEW;
END;
$$;
"""

"""Add durable approval requests, immutable decisions and permissions.

Revision ID: 0027_approval_control_plane
Revises: 0026_mcp_capability_discovery
Create Date: 2026-08-10
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027_approval_control_plane"
down_revision: str | None = "0026_mcp_capability_discovery"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY_EXPRESSION = (
    "tenant_id = nullif(current_setting('app.current_tenant_id', true), '')::uuid"
)
APPROVAL_PERMISSIONS = ("read", "list", "approve")


def upgrade() -> None:
    op.create_table(
        "approval_request",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_attempt", sa.Integer(), nullable=False),
        sa.Column("requester_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tool_call_id", sa.String(length=255), nullable=False),
        sa.Column("tool_name", sa.String(length=255), nullable=False),
        sa.Column("tool_schema_hash", sa.String(length=80), nullable=False),
        sa.Column("parameter_digest", sa.String(length=80), nullable=False),
        sa.Column("policy_version", sa.String(length=128), nullable=False),
        sa.Column("deployment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "resource_version",
            sa.BigInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "self_approval_allowed",
            sa.Boolean(),
            server_default=sa.text("false"),
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
        sa.CheckConstraint("execution_attempt >= 1", name="execution_attempt"),
        sa.CheckConstraint("length(tool_call_id) >= 1", name="tool_call_id"),
        sa.CheckConstraint("length(tool_name) >= 1", name="tool_name"),
        sa.CheckConstraint(
            "tool_schema_hash ~ '^sha256:[a-f0-9]{64}$'",
            name="tool_schema_hash",
        ),
        sa.CheckConstraint(
            "parameter_digest ~ '^sha256:[a-f0-9]{64}$'",
            name="parameter_digest",
        ),
        sa.CheckConstraint("length(policy_version) >= 1", name="policy_version"),
        sa.CheckConstraint(
            "status IN ('PENDING','APPROVED','REJECTED','EXPIRED','CANCELLED','CONSUMED')",
            name="status",
        ),
        sa.CheckConstraint("expires_at > created_at", name="expires_at"),
        sa.CheckConstraint("resource_version >= 1", name="resource_version"),
        sa.CheckConstraint("updated_at >= created_at", name="updated_at"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_approval_request__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["agent_run.tenant_id", "agent_run.id"],
            name="fk_approval_request__tenant_run__agent_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "requester_id"],
            ["tenant_member.tenant_id", "tenant_member.user_id"],
            name="fk_approval_request__tenant_requester__tenant_member",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "deployment_id"],
            ["deployment.tenant_id", "deployment.id"],
            name="fk_approval_request__tenant_deployment__deployment",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_approval_request"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_approval_request__tenant_id_id"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "execution_attempt",
            "tool_call_id",
            name="uq_approval_request__tenant_run_attempt_tool_call",
        ),
    )
    op.create_index(
        "ix_approval_request__tenant_status_expires_at",
        "approval_request",
        ["tenant_id", "status", "expires_at"],
    )
    op.create_index(
        "ix_approval_request__tenant_run_created_at",
        "approval_request",
        ["tenant_id", "run_id", sa.text("created_at DESC")],
    )
    op.create_table(
        "approval_decision",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("decision IN ('APPROVED','REJECTED')", name="decision"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_approval_decision__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "approval_id"],
            ["approval_request.tenant_id", "approval_request.id"],
            name="fk_approval_decision__tenant_approval__approval_request",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "actor_id"],
            ["tenant_member.tenant_id", "tenant_member.user_id"],
            name="fk_approval_decision__tenant_actor__tenant_member",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_approval_decision"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_approval_decision__tenant_id_id"
        ),
        sa.UniqueConstraint("approval_id", name="uq_approval_decision__approval_id"),
    )
    for table_name in ("approval_request", "approval_decision"):
        op.execute(sa.text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"CREATE POLICY tenant_isolation ON {table_name} "
                f"USING ({TENANT_POLICY_EXPRESSION}) "
                f"WITH CHECK ({TENANT_POLICY_EXPRESSION})"
            )
        )
    op.execute(sa.text(_APPROVAL_REQUEST_GUARD_FUNCTION))
    op.execute(sa.text(_APPROVAL_DECISION_GUARD_FUNCTION))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_approval_request__guard "
            "BEFORE UPDATE OR DELETE ON approval_request "
            "FOR EACH ROW EXECUTE FUNCTION guard_approval_request()"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_approval_decision__guard "
            "BEFORE INSERT OR UPDATE OR DELETE ON approval_decision "
            "FOR EACH ROW EXECUTE FUNCTION guard_approval_decision()"
        )
    )
    for action in APPROVAL_PERMISSIONS:
        op.execute(sa.text(f"""DO $$ DECLARE tenant_uuid uuid; BEGIN
                FOR tenant_uuid IN SELECT tenant_id FROM role
                WHERE built_in IS TRUE AND code = 'tenant_admin' LOOP
                    PERFORM set_config(
                        'app.current_tenant_id', tenant_uuid::text, true
                    );
                    INSERT INTO role_permission
                        (tenant_id, role_id, resource_type, action)
                    SELECT tenant_id, id, 'approval', '{action}' FROM role
                    WHERE tenant_id = tenant_uuid AND built_in IS TRUE
                    AND code = 'tenant_admin' ON CONFLICT DO NOTHING;
                END LOOP; END $$"""))


def downgrade() -> None:
    for action in APPROVAL_PERMISSIONS:
        op.execute(sa.text(f"""DO $$ DECLARE tenant_uuid uuid; BEGIN
                FOR tenant_uuid IN SELECT tenant_id FROM role
                WHERE built_in IS TRUE AND code = 'tenant_admin' LOOP
                    PERFORM set_config(
                        'app.current_tenant_id', tenant_uuid::text, true
                    );
                    DELETE FROM role_permission WHERE tenant_id = tenant_uuid
                    AND resource_type = 'approval' AND action = '{action}';
                END LOOP; END $$"""))
    op.execute(
        sa.text("DROP TRIGGER trg_approval_decision__guard ON approval_decision")
    )
    op.execute(sa.text("DROP FUNCTION guard_approval_decision()"))
    op.execute(sa.text("DROP TRIGGER trg_approval_request__guard ON approval_request"))
    op.execute(sa.text("DROP FUNCTION guard_approval_request()"))
    for table_name in ("approval_decision", "approval_request"):
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table_name}"))
        op.execute(sa.text(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY"))
    op.drop_table("approval_decision")
    op.drop_index(
        "ix_approval_request__tenant_run_created_at", table_name="approval_request"
    )
    op.drop_index(
        "ix_approval_request__tenant_status_expires_at",
        table_name="approval_request",
    )
    op.drop_table("approval_request")


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


_APPROVAL_DECISION_GUARD_FUNCTION = """
CREATE FUNCTION guard_approval_decision() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE request_row approval_request%ROWTYPE;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'approval decisions are immutable facts';
    END IF;
    SELECT * INTO request_row FROM approval_request
    WHERE tenant_id = NEW.tenant_id AND id = NEW.approval_id
    FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'approval request is unavailable';
    END IF;
    IF request_row.status <> NEW.decision THEN
        RAISE EXCEPTION 'approval decision does not match request state';
    END IF;
    IF request_row.expires_at <= NEW.created_at THEN
        RAISE EXCEPTION 'expired approval request cannot be decided';
    END IF;
    IF NOT request_row.self_approval_allowed
       AND request_row.requester_id = NEW.actor_id THEN
        RAISE EXCEPTION 'approval requester cannot decide this request';
    END IF;
    RETURN NEW;
END;
$$;
"""

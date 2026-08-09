"""Add tenant-scoped Runs, attempts and immutable execution inputs.

Revision ID: 0017_agent_run
Revises: 0016_chat_message
Create Date: 2026-08-08
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_agent_run"
down_revision: str | None = "0016_chat_message"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY_EXPRESSION = (
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)
RUN_PERMISSIONS = ("create", "read", "list")


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_deployment__tenant_id_agent_snapshot",
        "deployment",
        ["tenant_id", "id", "agent_id", "snapshot_id"],
    )
    op.create_table(
        "agent_run",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assistant_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deployment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default=sa.text("'CREATED'"),
            nullable=False,
        ),
        sa.Column("result_quality", sa.String(length=32), nullable=True),
        sa.Column(
            "current_attempt", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "latest_sequence_no",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("client_request_id", sa.String(length=128), nullable=True),
        sa.Column("retry_of_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "timeout_seconds",
            sa.Integer(),
            server_default=sa.text("600"),
            nullable=False,
        ),
        sa.Column("token_budget", sa.BigInteger(), nullable=True),
        sa.Column("cost_budget_amount", sa.Numeric(20, 8), nullable=True),
        sa.Column("cost_budget_currency", sa.String(length=3), nullable=True),
        sa.Column("workflow_id", sa.String(length=255), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('CREATED','QUEUED','PREPARING','RUNNING',"
            "'WAITING_APPROVAL','CANCELLING','SUCCEEDED','FAILED','CANCELLED','TIMEOUT')",
            name="status",
        ),
        sa.CheckConstraint(
            "result_quality IS NULL OR result_quality IN "
            "('NORMAL','SUCCEEDED_WITH_WARNINGS')",
            name="result_quality",
        ),
        sa.CheckConstraint("current_attempt >= 0", name="current_attempt"),
        sa.CheckConstraint("latest_sequence_no >= 0", name="latest_sequence_no"),
        sa.CheckConstraint(
            "timeout_seconds BETWEEN 1 AND 86400", name="timeout_seconds"
        ),
        sa.CheckConstraint(
            "token_budget IS NULL OR token_budget >= 1", name="token_budget"
        ),
        sa.CheckConstraint(
            "(cost_budget_amount IS NULL AND cost_budget_currency IS NULL) OR "
            "(cost_budget_amount >= 0 AND cost_budget_currency ~ '^[A-Z]{3}$')",
            name="cost_budget",
        ),
        sa.CheckConstraint(
            "error_detail_json IS NULL OR jsonb_typeof(error_detail_json) = 'object'",
            name="error_detail_json",
        ),
        sa.CheckConstraint(
            "(status = 'CREATED' AND queued_at IS NULL AND started_at IS NULL "
            "AND finished_at IS NULL) OR status <> 'CREATED'",
            name="created_timestamps",
        ),
        sa.CheckConstraint(
            "queued_at IS NULL OR queued_at >= created_at", name="queued_at"
        ),
        sa.CheckConstraint(
            "started_at IS NULL OR started_at >= created_at", name="started_at"
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= created_at", name="finished_at"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_agent_run__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["chat_session.tenant_id", "chat_session.id"],
            name="fk_agent_run__tenant_session__chat_session",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_message_id", "session_id"],
            ["chat_message.tenant_id", "chat_message.id", "chat_message.session_id"],
            name="fk_agent_run__tenant_user_message_session__chat_message",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "assistant_message_id", "session_id"],
            ["chat_message.tenant_id", "chat_message.id", "chat_message.session_id"],
            name="fk_agent_run__tenant_assistant_message_session__chat_message",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "deployment_id", "agent_id", "snapshot_id"],
            [
                "deployment.tenant_id",
                "deployment.id",
                "deployment.agent_id",
                "deployment.snapshot_id",
            ],
            name="fk_agent_run__tenant_deployment_agent_snapshot__deployment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "snapshot_id"],
            ["agent_snapshot.tenant_id", "agent_snapshot.id"],
            name="fk_agent_run__tenant_snapshot__agent_snapshot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "created_by"],
            ["tenant_member.tenant_id", "tenant_member.user_id"],
            name="fk_agent_run__tenant_created_by__tenant_member",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "retry_of_run_id", "session_id"],
            ["agent_run.tenant_id", "agent_run.id", "agent_run.session_id"],
            name="fk_agent_run__tenant_retry_session__agent_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_run"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_agent_run__tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            "session_id",
            name="uq_agent_run__tenant_id_session_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "created_by",
            "idempotency_key",
            name="uq_agent_run__tenant_created_by_idempotency",
        ),
    )
    op.create_index(
        "ix_agent_run__tenant_session_created_at",
        "agent_run",
        ["tenant_id", "session_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_agent_run__tenant_status_created_at",
        "agent_run",
        ["tenant_id", "status", sa.text("created_at DESC")],
    )
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_agent_run__active_session_branch ON agent_run "
            "(tenant_id, session_id, COALESCE(branch_id, "
            "'00000000-0000-0000-0000-000000000000'::uuid)) "
            "WHERE status NOT IN ('SUCCEEDED','FAILED','CANCELLED','TIMEOUT')"
        )
    )

    op.create_table(
        "run_attempt",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("fencing_token_hash", sa.String(length=80), nullable=False),
        sa.Column("worker_id", sa.String(length=255), nullable=True),
        sa.Column("runtime_handle_ref", sa.String(length=2048), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint("attempt_no >= 1", name="attempt_no"),
        sa.CheckConstraint(
            "fencing_token_hash ~ '^sha256:[a-f0-9]{64}$'",
            name="fencing_token_hash",
        ),
        sa.CheckConstraint(
            "status IN ('ALLOCATED','STARTING','RUNNING','COMPLETED','LOST','CANCELLED')",
            name="status",
        ),
        sa.CheckConstraint(
            "heartbeat_at IS NULL OR started_at IS NOT NULL", name="heartbeat_started"
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR started_at IS NOT NULL", name="finished_started"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_run_attempt__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["agent_run.tenant_id", "agent_run.id"],
            name="fk_run_attempt__tenant_run__agent_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_run_attempt"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_run_attempt__tenant_id_id"),
        sa.UniqueConstraint("run_id", "attempt_no", name="uq_run_attempt__run_attempt"),
    )
    op.create_index(
        "ix_run_attempt__tenant_run_status",
        "run_attempt",
        ["tenant_id", "run_id", "status"],
    )
    op.create_foreign_key(
        "fk_chat_message__tenant_source_run_session__agent_run",
        "chat_message",
        "agent_run",
        ["tenant_id", "source_run_id", "session_id"],
        ["tenant_id", "id", "session_id"],
        ondelete="RESTRICT",
    )

    for table_name in ("agent_run", "run_attempt"):
        op.execute(sa.text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"CREATE POLICY tenant_isolation ON {table_name} "
                f"USING ({TENANT_POLICY_EXPRESSION}) "
                f"WITH CHECK ({TENANT_POLICY_EXPRESSION})"
            )
        )

    op.execute(sa.text(_RUN_GUARD_FUNCTION))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_agent_run__guard BEFORE INSERT OR UPDATE OR DELETE "
            "ON agent_run FOR EACH ROW EXECUTE FUNCTION guard_agent_run_mutation()"
        )
    )
    op.execute(sa.text(_RUN_ATTEMPT_GUARD_FUNCTION))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_run_attempt__guard BEFORE UPDATE OR DELETE "
            "ON run_attempt FOR EACH ROW EXECUTE FUNCTION guard_run_attempt_mutation()"
        )
    )

    for action in RUN_PERMISSIONS:
        op.execute(sa.text(f"""DO $$ DECLARE tenant_uuid uuid; BEGIN
                FOR tenant_uuid IN SELECT tenant_id FROM role
                WHERE built_in IS TRUE AND code = 'tenant_admin' LOOP
                    PERFORM set_config(
                        'app.current_tenant_id', tenant_uuid::text, true
                    );
                    INSERT INTO role_permission
                        (tenant_id, role_id, resource_type, action)
                    SELECT tenant_id, id, 'run', '{action}' FROM role
                    WHERE tenant_id = tenant_uuid AND built_in IS TRUE
                    AND code = 'tenant_admin' ON CONFLICT DO NOTHING;
                END LOOP; END $$"""))


def downgrade() -> None:
    for action in RUN_PERMISSIONS:
        op.execute(sa.text(f"""DO $$ DECLARE tenant_uuid uuid; BEGIN
                FOR tenant_uuid IN SELECT tenant_id FROM role
                WHERE built_in IS TRUE AND code = 'tenant_admin' LOOP
                    PERFORM set_config(
                        'app.current_tenant_id', tenant_uuid::text, true
                    );
                    DELETE FROM role_permission WHERE tenant_id = tenant_uuid
                    AND resource_type = 'run' AND action = '{action}';
                END LOOP; END $$"""))
    op.execute(sa.text("DROP TRIGGER trg_run_attempt__guard ON run_attempt"))
    op.execute(sa.text("DROP FUNCTION guard_run_attempt_mutation()"))
    op.execute(sa.text("DROP TRIGGER trg_agent_run__guard ON agent_run"))
    op.execute(sa.text("DROP FUNCTION guard_agent_run_mutation()"))
    op.drop_constraint(
        "fk_chat_message__tenant_source_run_session__agent_run",
        "chat_message",
        type_="foreignkey",
    )
    for table_name in ("run_attempt", "agent_run"):
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table_name}"))
        op.execute(sa.text(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY"))
    op.drop_index("ix_run_attempt__tenant_run_status", table_name="run_attempt")
    op.drop_table("run_attempt")
    op.drop_index("uq_agent_run__active_session_branch", table_name="agent_run")
    op.drop_index("ix_agent_run__tenant_status_created_at", table_name="agent_run")
    op.drop_index("ix_agent_run__tenant_session_created_at", table_name="agent_run")
    op.drop_table("agent_run")
    op.drop_constraint(
        "uq_deployment__tenant_id_agent_snapshot", "deployment", type_="unique"
    )


_RUN_GUARD_FUNCTION = """
CREATE FUNCTION guard_agent_run_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE linked_role text; linked_source uuid;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'agent_run facts cannot be deleted';
    END IF;
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
            (OLD.status = 'QUEUED' AND NEW.status IN ('PREPARING','CANCELLING','TIMEOUT')) OR
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
END; $$
"""

_RUN_ATTEMPT_GUARD_FUNCTION = """
CREATE FUNCTION guard_run_attempt_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'run_attempt facts cannot be deleted';
    END IF;
    IF ROW(NEW.tenant_id, NEW.run_id, NEW.attempt_no, NEW.fencing_token_hash)
       IS DISTINCT FROM
       ROW(OLD.tenant_id, OLD.run_id, OLD.attempt_no, OLD.fencing_token_hash) THEN
        RAISE EXCEPTION 'run_attempt identity is immutable';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'ALLOCATED' AND NEW.status IN ('STARTING','LOST','CANCELLED')) OR
        (OLD.status = 'STARTING' AND NEW.status IN ('RUNNING','LOST','CANCELLED')) OR
        (OLD.status = 'RUNNING' AND NEW.status IN ('COMPLETED','LOST','CANCELLED'))
    ) THEN
        RAISE EXCEPTION 'invalid run_attempt state transition: % -> %', OLD.status, NEW.status;
    END IF;
    RETURN NEW;
END; $$
"""

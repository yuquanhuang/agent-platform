"""Add immutable RunEvent facts and per-Run sequence counters.

Revision ID: 0020_run_event_store
Revises: 0019_run_workflow_reconciliation
Create Date: 2026-08-08
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020_run_event_store"
down_revision: str | None = "0019_run_workflow_reconciliation"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY_EXPRESSION = (
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        "run_event",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_no", sa.BigInteger(), nullable=False),
        sa.Column("source_event_id", sa.String(length=255), nullable=False),
        sa.Column("execution_attempt", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload_version", sa.String(length=16), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.CheckConstraint("sequence_no >= 1", name="sequence_no"),
        sa.CheckConstraint("execution_attempt >= 1", name="execution_attempt"),
        sa.CheckConstraint("schema_version = '1.0'", name="schema_version"),
        sa.CheckConstraint("payload_version = '1.0'", name="payload_version"),
        sa.CheckConstraint(
            "event_type IN ('run_created','run_queued','run_started',"
            "'text_message_start','text_delta','text_message_end','thinking_delta',"
            "'plan_updated','tool_call_start','tool_call_args','tool_call_result',"
            "'approval_required','approval_resolved','task_progress',"
            "'artifact_created','warning','run_succeeded','run_failed',"
            "'run_cancelled','run_timeout')",
            name="event_type",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload_json) = 'object'", name="payload_json"
        ),
        sa.CheckConstraint(
            "octet_length(payload_json::text) <= 262144", name="payload_size"
        ),
        sa.CheckConstraint("length(source_event_id) >= 1", name="source_event_id"),
        sa.CheckConstraint("length(trace_id) >= 3", name="trace_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_run_event__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "session_id"],
            ["agent_run.tenant_id", "agent_run.id", "agent_run.session_id"],
            name="fk_run_event__tenant_run_session__agent_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_run_event"),
        sa.UniqueConstraint("run_id", "sequence_no", name="uq_run_event__run_sequence"),
        sa.UniqueConstraint(
            "run_id",
            "execution_attempt",
            "source_event_id",
            name="uq_run_event__run_attempt_source",
        ),
    )
    op.create_index(
        "ix_run_event__tenant_run_sequence",
        "run_event",
        ["tenant_id", "run_id", "sequence_no"],
    )
    op.create_index(
        "ix_run_event__tenant_type_recorded_at",
        "run_event",
        ["tenant_id", "event_type", "recorded_at"],
    )

    op.create_table(
        "run_event_counter",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "next_sequence_no",
            sa.BigInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.CheckConstraint("next_sequence_no >= 1", name="next_sequence_no"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_run_event_counter__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["agent_run.tenant_id", "agent_run.id"],
            name="fk_run_event_counter__tenant_run__agent_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", name="pk_run_event_counter"),
    )
    op.create_index(
        "ix_run_event_counter__tenant_run",
        "run_event_counter",
        ["tenant_id", "run_id"],
    )

    for table_name in ("run_event", "run_event_counter"):
        op.execute(sa.text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"CREATE POLICY tenant_isolation ON {table_name} "
                f"USING ({TENANT_POLICY_EXPRESSION}) "
                f"WITH CHECK ({TENANT_POLICY_EXPRESSION})"
            )
        )

    op.execute(sa.text(_RUN_EVENT_IMMUTABILITY_FUNCTION))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_run_event__immutable BEFORE UPDATE OR DELETE "
            "ON run_event FOR EACH ROW EXECUTE FUNCTION reject_run_event_mutation()"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER trg_run_event__immutable ON run_event"))
    op.execute(sa.text("DROP FUNCTION reject_run_event_mutation()"))
    for table_name in ("run_event_counter", "run_event"):
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table_name}"))
        op.execute(sa.text(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY"))
    op.drop_index("ix_run_event_counter__tenant_run", table_name="run_event_counter")
    op.drop_table("run_event_counter")
    op.drop_index("ix_run_event__tenant_type_recorded_at", table_name="run_event")
    op.drop_index("ix_run_event__tenant_run_sequence", table_name="run_event")
    op.drop_table("run_event")


_RUN_EVENT_IMMUTABILITY_FUNCTION = """
CREATE FUNCTION reject_run_event_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'run_event facts are immutable';
END; $$
"""

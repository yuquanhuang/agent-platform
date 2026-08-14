"""Add trusted model cost admission and append-only ledger facts.

Revision ID: 0042_model_cost_budget
Revises: 0041_capacity_domain_lease
Create Date: 2026-08-13
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0042_model_cost_budget"
down_revision: str | None = "0041_capacity_domain_lease"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY = (
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)


def upgrade() -> None:
    op.add_column(
        "price_catalog_version",
        sa.Column("status", sa.String(20), server_default="PUBLISHED", nullable=False),
    )
    op.add_column(
        "price_catalog_version",
        sa.Column("source_digest", sa.String(80), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE price_catalog_version SET source_digest = content_hash "
            "WHERE source_digest IS NULL"
        )
    )
    op.alter_column("price_catalog_version", "source_digest", nullable=False)
    op.drop_constraint("ck_price_catalog_version__currency", "price_catalog_version")
    op.create_check_constraint(
        "ck_price_catalog_version__currency",
        "price_catalog_version",
        "currency IN ('USD', 'CNY')",
    )
    op.create_check_constraint(
        "ck_price_catalog_version__status",
        "price_catalog_version",
        "status IN ('DRAFT', 'PUBLISHED')",
    )
    op.create_check_constraint(
        "ck_price_catalog_version__source_digest",
        "price_catalog_version",
        "source_digest ~ '^sha256:[a-f0-9]{64}$'",
    )
    op.drop_constraint("ck_budget_policy_version__enforcement", "budget_policy_version")
    op.create_check_constraint(
        "ck_budget_policy_version__enforcement",
        "budget_policy_version",
        "enforcement IN ('HARD', 'SOFT')",
    )
    op.drop_constraint(
        "ck_budget_policy_version__cost_currency", "budget_policy_version"
    )
    op.create_check_constraint(
        "ck_budget_policy_version__cost_currency",
        "budget_policy_version",
        "cost_limit_currency IS NULL OR cost_limit_currency IN ('USD', 'CNY')",
    )

    for name, column in (
        ("max_output_tokens", sa.BigInteger()),
        ("max_reasoning_tokens", sa.BigInteger()),
        ("counter_profile_id", sa.String(128)),
        ("counter_profile_version", sa.String(64)),
        ("counter_profile_hash", sa.String(80)),
        ("billing_semantics_version", sa.String(64)),
    ):
        op.add_column("model_binding_snapshot", sa.Column(name, column, nullable=True))
    op.create_check_constraint(
        "ck_model_binding_snapshot__output_cap",
        "model_binding_snapshot",
        "max_output_tokens IS NULL OR max_output_tokens >= 1",
    )
    op.create_check_constraint(
        "ck_model_binding_snapshot__reasoning_cap",
        "model_binding_snapshot",
        "max_reasoning_tokens IS NULL OR max_reasoning_tokens >= 1",
    )
    op.create_check_constraint(
        "ck_model_binding_snapshot__counter_profile",
        "model_binding_snapshot",
        "(counter_profile_id IS NULL AND counter_profile_version IS NULL "
        "AND counter_profile_hash IS NULL) OR (counter_profile_id IS NOT NULL "
        "AND counter_profile_version IS NOT NULL AND counter_profile_hash IS NOT NULL "
        "AND counter_profile_hash ~ '^sha256:[a-f0-9]{64}$')",
    )

    for name, column in (
        ("reserved_cost_amount", sa.Numeric(28, 8)),
        ("reserved_cost_currency", sa.String(3)),
        ("canonical_input_hash", sa.String(80)),
        ("counter_profile_id", sa.String(128)),
        ("counter_profile_version", sa.String(64)),
        ("counter_profile_hash", sa.String(80)),
        ("upper_bound_json", postgresql.JSONB()),
    ):
        op.add_column("budget_reservation", sa.Column(name, column, nullable=True))
    op.create_check_constraint(
        "ck_budget_reservation__cost_pair",
        "budget_reservation",
        "(reserved_cost_amount IS NULL) = (reserved_cost_currency IS NULL)",
    )
    op.create_check_constraint(
        "ck_budget_reservation__cost_currency",
        "budget_reservation",
        "reserved_cost_currency IS NULL OR reserved_cost_currency IN ('USD', 'CNY')",
    )

    op.create_table(
        "cost_ledger_entry",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reservation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("budget_policy_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "budget_policy_version_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "price_catalog_version_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("entry_type", sa.String(20), nullable=False),
        sa.Column("amount", sa.Numeric(28, 8), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("period_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.String(255), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("agent_id", sa.String(255), nullable=False),
        sa.Column("model_binding_id", sa.String(255), nullable=False),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("model", sa.String(255), nullable=True),
        sa.Column("provider_attempt_no", sa.Integer(), nullable=True),
        sa.Column("details_json", postgresql.JSONB(), nullable=False),
        sa.Column("entry_hash", sa.String(80), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "entry_type IN ('RESERVE', 'RELEASE', 'SETTLE', 'ADJUST', 'UNKNOWN')",
            name="entry_type",
        ),
        sa.CheckConstraint("currency IN ('USD', 'CNY')", name="currency"),
        sa.CheckConstraint("amount >= 0", name="amount"),
        sa.CheckConstraint("period_started_at < period_ends_at", name="period_range"),
        sa.CheckConstraint("entry_hash ~ '^sha256:[a-f0-9]{64}$'", name="entry_hash"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["reservation_id"], ["budget_reservation.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_cost_ledger__tenant_period_currency",
        "cost_ledger_entry",
        ["tenant_id", "period_started_at", "currency", "entry_type"],
    )
    op.create_index(
        "ix_cost_ledger__tenant_run_created",
        "cost_ledger_entry",
        ["tenant_id", "run_id", "created_at"],
    )

    op.create_table(
        "model_provider_attempt",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reservation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("run_id", sa.String(255), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("provider_request_id", sa.String(512), nullable=True),
        sa.Column("submission_state", sa.String(20), nullable=False),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "submission_state IN ('submitted', 'unknown')", name="submission_state"
        ),
        sa.CheckConstraint("attempt_no >= 1", name="attempt_no"),
        sa.CheckConstraint("finished_at >= started_at", name="time_order"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["reservation_id"], ["budget_reservation.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "idempotency_key",
            "attempt_no",
            name="uq_model_provider_attempt__request_attempt",
        ),
    )
    op.create_index(
        "ix_model_provider_attempt__tenant_run",
        "model_provider_attempt",
        ["tenant_id", "run_id", "created_at"],
    )
    for table in ("cost_ledger_entry", "model_provider_attempt"):
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_POLICY}) WITH CHECK ({TENANT_POLICY})"
            )
        )
        op.execute(
            sa.text(
                f"CREATE TRIGGER trg_{table}__immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION guard_model_usage_immutable()"
            )
        )


def downgrade() -> None:
    for table in ("model_provider_attempt", "cost_ledger_entry"):
        op.execute(sa.text(f"DROP TRIGGER trg_{table}__immutable ON {table}"))
        op.drop_table(table)
    for name in (
        "upper_bound_json",
        "counter_profile_hash",
        "counter_profile_version",
        "counter_profile_id",
        "canonical_input_hash",
        "reserved_cost_currency",
        "reserved_cost_amount",
    ):
        op.drop_column("budget_reservation", name)
    for name in (
        "billing_semantics_version",
        "counter_profile_hash",
        "counter_profile_version",
        "counter_profile_id",
        "max_reasoning_tokens",
        "max_output_tokens",
    ):
        op.drop_column("model_binding_snapshot", name)
    op.drop_constraint(
        "ck_budget_policy_version__cost_currency", "budget_policy_version"
    )
    op.create_check_constraint(
        "ck_budget_policy_version__cost_currency",
        "budget_policy_version",
        "cost_limit_currency IS NULL OR cost_limit_currency ~ '^[A-Z]{3}$'",
    )
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM budget_policy_version "
            "WHERE enforcement = 'SOFT') THEN "
            "RAISE EXCEPTION 'cannot downgrade 0042 while SOFT budget policy versions exist'; "
            "END IF; END $$"
        )
    )
    op.drop_constraint("ck_budget_policy_version__enforcement", "budget_policy_version")
    op.create_check_constraint(
        "ck_budget_policy_version__enforcement",
        "budget_policy_version",
        "enforcement = 'HARD'",
    )
    op.drop_constraint(
        "ck_price_catalog_version__source_digest", "price_catalog_version"
    )
    op.drop_constraint("ck_price_catalog_version__status", "price_catalog_version")
    op.drop_constraint("ck_price_catalog_version__currency", "price_catalog_version")
    op.create_check_constraint(
        "ck_price_catalog_version__currency",
        "price_catalog_version",
        "currency ~ '^[A-Z]{3}$'",
    )
    op.drop_column("price_catalog_version", "source_digest")
    op.drop_column("price_catalog_version", "status")

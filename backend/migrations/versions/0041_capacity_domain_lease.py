"""Add durable Run capacity domains and leases.

Revision ID: 0041_capacity_domain_lease
Revises: 0040_artifact_retention
Create Date: 2026-08-13
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041_capacity_domain_lease"
down_revision: str | None = "0040_artifact_retention"
branch_labels: str | None = None
depends_on: str | None = None

PLATFORM_POLICY = "current_setting('app.platform_context', true) = 'true'"
LEASE_POLICY = (
    "current_setting('app.platform_context', true) = 'true' OR "
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)


def upgrade() -> None:
    # Dropping a trigger takes an ACCESS EXCLUSIVE table lock for the migration
    # transaction. This gives the backfill a bounded maintenance window while
    # keeping the 0038 immutable-fact guard unchanged outside this migration.
    op.execute(
        sa.text("DROP TRIGGER trg_run_admission_queue__guard ON run_admission_queue")
    )
    op.drop_constraint(
        "ck_run_admission_queue__capacity_domain",
        "run_admission_queue",
        type_="check",
    )
    op.alter_column(
        "run_admission_queue",
        "capacity_domain",
        existing_type=sa.String(length=128),
        type_=sa.String(length=255),
        existing_nullable=False,
    )
    op.create_check_constraint(
        "capacity_domain",
        "run_admission_queue",
        "length(capacity_domain) BETWEEN 1 AND 255",
    )
    op.execute(
        sa.text(
            "UPDATE run_admission_queue AS q SET capacity_domain = d.runtime_target_id "
            "FROM agent_run AS r JOIN deployment AS d "
            "ON d.tenant_id = r.tenant_id AND d.id = r.deployment_id "
            "WHERE r.tenant_id = q.tenant_id AND r.id = q.run_id"
        )
    )
    op.execute(sa.text(_CREATE_QUEUE_GUARD_TRIGGER))

    op.create_table(
        "run_capacity_domain",
        sa.Column("domain_key", sa.String(length=255), nullable=False),
        sa.Column("configured_slots", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'DRAINING'"),
            nullable=False,
        ),
        sa.Column("config_hash", sa.String(length=80), nullable=False),
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
        sa.CheckConstraint("length(domain_key) BETWEEN 1 AND 255", name="domain_key"),
        sa.CheckConstraint(
            "configured_slots BETWEEN 1 AND 1000000", name="configured_slots"
        ),
        sa.CheckConstraint("status IN ('ACTIVE','DRAINING','DISABLED')", name="status"),
        sa.CheckConstraint(
            "config_hash ~ '^sha256:[a-f0-9]{64}$' OR "
            "config_hash = 'migration:unconfigured'",
            name="config_hash",
        ),
        sa.CheckConstraint("resource_version >= 1", name="resource_version"),
        sa.PrimaryKeyConstraint("domain_key", name="pk_run_capacity_domain"),
    )
    op.create_index(
        "ix_run_capacity_domain__status_domain",
        "run_capacity_domain",
        ["status", "domain_key"],
    )

    op.create_table(
        "run_capacity_lease",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("domain_key", sa.String(length=255), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("renewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.String(length=32), nullable=True),
        sa.Column(
            "resource_version",
            sa.BigInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.CheckConstraint("expires_at > acquired_at", name="expires_at"),
        sa.CheckConstraint("renewed_at >= acquired_at", name="renewed_at"),
        sa.CheckConstraint(
            "(released_at IS NULL AND release_reason IS NULL) OR "
            "(released_at IS NOT NULL AND released_at >= acquired_at AND "
            "release_reason IN ('RUN_TERMINAL','ORPHANED'))",
            name="release_state",
        ),
        sa.CheckConstraint("resource_version >= 1", name="resource_version"),
        sa.ForeignKeyConstraint(
            ["domain_key"],
            ["run_capacity_domain.domain_key"],
            name="fk_run_capacity_lease__domain_key__run_capacity_domain",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_run_capacity_lease__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["agent_run.tenant_id", "agent_run.id"],
            name="fk_run_capacity_lease__tenant_run__agent_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_run_capacity_lease"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_run_capacity_lease__tenant_id_id"
        ),
        sa.UniqueConstraint("run_id", name="uq_run_capacity_lease__run_id"),
    )
    op.create_index(
        "ix_run_capacity_lease__domain_active",
        "run_capacity_lease",
        ["domain_key", "acquired_at", "id"],
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.create_index(
        "ix_run_capacity_lease__tenant_expires",
        "run_capacity_lease",
        ["tenant_id", "expires_at", "id"],
        postgresql_where=sa.text("released_at IS NULL"),
    )

    op.execute(
        sa.text(
            "INSERT INTO run_capacity_domain "
            "(domain_key, configured_slots, status, config_hash) "
            "SELECT q.capacity_domain, GREATEST(count(*), 1)::integer, "
            "'DRAINING', 'migration:unconfigured' "
            "FROM run_admission_queue AS q JOIN agent_run AS r "
            "ON r.tenant_id = q.tenant_id AND r.id = q.run_id "
            "WHERE q.status = 'ADMITTED' AND r.status NOT IN "
            "('SUCCEEDED','FAILED','CANCELLED','TIMEOUT') "
            "GROUP BY q.capacity_domain ON CONFLICT (domain_key) DO NOTHING"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO run_capacity_lease "
            "(id, domain_key, tenant_id, run_id, acquired_at, renewed_at, expires_at) "
            "SELECT gen_random_uuid(), q.capacity_domain, q.tenant_id, q.run_id, "
            "COALESCE(q.admitted_at, q.updated_at), now(), now() + interval '5 minutes' "
            "FROM run_admission_queue AS q JOIN agent_run AS r "
            "ON r.tenant_id = q.tenant_id AND r.id = q.run_id "
            "WHERE q.status = 'ADMITTED' AND r.status NOT IN "
            "('SUCCEEDED','FAILED','CANCELLED','TIMEOUT') "
            "ON CONFLICT (run_id) DO NOTHING"
        )
    )

    op.execute(sa.text("ALTER TABLE run_capacity_domain ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE run_capacity_domain FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY platform_scheduler ON run_capacity_domain "
            f"USING ({PLATFORM_POLICY}) WITH CHECK ({PLATFORM_POLICY})"
        )
    )
    op.execute(sa.text("ALTER TABLE run_capacity_lease ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE run_capacity_lease FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON run_capacity_lease "
            f"USING ({LEASE_POLICY}) WITH CHECK ({LEASE_POLICY})"
        )
    )
    op.execute(sa.text("DROP POLICY tenant_isolation ON run_admission_queue"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON run_admission_queue "
            f"USING ({LEASE_POLICY}) WITH CHECK ({LEASE_POLICY})"
        )
    )
    op.execute(sa.text(_DOMAIN_GUARD_FUNCTION))
    op.execute(sa.text(_LEASE_GUARD_FUNCTION))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_run_capacity_domain__guard BEFORE UPDATE OR DELETE "
            "ON run_capacity_domain FOR EACH ROW EXECUTE FUNCTION guard_run_capacity_domain()"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_run_capacity_lease__guard BEFORE UPDATE OR DELETE "
            "ON run_capacity_lease FOR EACH ROW EXECUTE FUNCTION guard_run_capacity_lease()"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("DROP TRIGGER trg_run_capacity_lease__guard ON run_capacity_lease")
    )
    op.execute(
        sa.text("DROP TRIGGER trg_run_capacity_domain__guard ON run_capacity_domain")
    )
    op.execute(sa.text("DROP FUNCTION guard_run_capacity_lease()"))
    op.execute(sa.text("DROP FUNCTION guard_run_capacity_domain()"))
    op.execute(sa.text("DROP POLICY tenant_isolation ON run_admission_queue"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON run_admission_queue USING "
            "(tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid) "
            "WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)"
        )
    )
    op.execute(sa.text("DROP POLICY tenant_isolation ON run_capacity_lease"))
    op.execute(sa.text("DROP POLICY platform_scheduler ON run_capacity_domain"))
    op.drop_index(
        "ix_run_capacity_lease__tenant_expires", table_name="run_capacity_lease"
    )
    op.drop_index(
        "ix_run_capacity_lease__domain_active", table_name="run_capacity_lease"
    )
    op.drop_table("run_capacity_lease")
    op.drop_index(
        "ix_run_capacity_domain__status_domain", table_name="run_capacity_domain"
    )
    op.drop_table("run_capacity_domain")
    op.execute(
        sa.text("DROP TRIGGER trg_run_admission_queue__guard ON run_admission_queue")
    )
    op.execute(
        sa.text(
            "UPDATE run_admission_queue SET capacity_domain = "
            "'tenant/' || tenant_id::text"
        )
    )
    op.drop_constraint(
        "ck_run_admission_queue__capacity_domain",
        "run_admission_queue",
        type_="check",
    )
    op.alter_column(
        "run_admission_queue",
        "capacity_domain",
        existing_type=sa.String(length=255),
        type_=sa.String(length=128),
        existing_nullable=False,
    )
    op.create_check_constraint(
        "capacity_domain",
        "run_admission_queue",
        "length(capacity_domain) BETWEEN 1 AND 128",
    )
    op.execute(sa.text(_CREATE_QUEUE_GUARD_TRIGGER))


_CREATE_QUEUE_GUARD_TRIGGER = """
CREATE TRIGGER trg_run_admission_queue__guard
BEFORE UPDATE OR DELETE ON run_admission_queue FOR EACH ROW
EXECUTE FUNCTION guard_run_admission_queue_mutation()
"""


_DOMAIN_GUARD_FUNCTION = """
CREATE FUNCTION guard_run_capacity_domain() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'run capacity domains cannot be deleted';
    END IF;
    IF NEW.domain_key IS DISTINCT FROM OLD.domain_key OR
       NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'run capacity domain identity is immutable';
    END IF;
    IF NEW.resource_version <> OLD.resource_version + 1 THEN
        RAISE EXCEPTION 'run capacity domain resource_version must increment by one';
    END IF;
    RETURN NEW;
END $$;
"""


_LEASE_GUARD_FUNCTION = """
CREATE FUNCTION guard_run_capacity_lease() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'run capacity leases cannot be deleted';
    END IF;
    IF ROW(NEW.id, NEW.domain_key, NEW.tenant_id, NEW.run_id, NEW.acquired_at)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.domain_key, OLD.tenant_id, OLD.run_id, OLD.acquired_at) THEN
        RAISE EXCEPTION 'run capacity lease binding facts are immutable';
    END IF;
    IF OLD.released_at IS NOT NULL AND
       ROW(NEW.released_at, NEW.release_reason) IS DISTINCT FROM
       ROW(OLD.released_at, OLD.release_reason) THEN
        RAISE EXCEPTION 'run capacity lease cannot be reopened or released twice';
    END IF;
    IF NEW.expires_at < OLD.expires_at OR NEW.renewed_at < OLD.renewed_at THEN
        RAISE EXCEPTION 'run capacity lease renewal cannot move backwards';
    END IF;
    IF NEW.resource_version <> OLD.resource_version + 1 THEN
        RAISE EXCEPTION 'run capacity lease resource_version must increment by one';
    END IF;
    RETURN NEW;
END $$;
"""

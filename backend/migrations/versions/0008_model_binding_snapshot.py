"""Freeze immutable provider bindings with Model Config versions.

Revision ID: 0008_model_binding_snapshot
Revises: 0007_model_gateway_usage
Create Date: 2026-08-07
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_model_binding_snapshot"
down_revision: str | None = "0007_model_gateway_usage"
branch_labels: str | None = None
depends_on: str | None = None

TENANT_POLICY_EXPRESSION = (
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_resource_version__tenant_id_id",
        "resource_version",
        ["tenant_id", "id"],
    )
    op.create_table(
        "model_binding_snapshot",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "model_config_definition_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "model_config_version_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "provider_definition_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("provider_type", sa.String(length=64), nullable=False),
        sa.Column("base_url", sa.String(length=2048), nullable=False),
        sa.Column("secret_ref", sa.String(length=512), nullable=False),
        sa.Column("provider_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.String(length=255), nullable=False),
        sa.Column("capabilities_json", postgresql.JSONB(), nullable=False),
        sa.Column("default_parameters_json", postgresql.JSONB(), nullable=False),
        sa.Column("max_context_tokens", sa.BigInteger(), nullable=True),
        sa.Column("rate_limit_rpm", sa.Integer(), nullable=True),
        sa.Column("snapshot_hash", sa.String(length=80), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "provider_type IN ('openai', 'qwen', 'deepseek')",
            name="provider_type",
        ),
        sa.CheckConstraint(
            "provider_timeout_seconds BETWEEN 1 AND 600",
            name="timeout",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(capabilities_json) = 'array'",
            name="capabilities_json",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(default_parameters_json) = 'object'",
            name="default_parameters_json",
        ),
        sa.CheckConstraint(
            "max_context_tokens IS NULL OR max_context_tokens >= 1",
            name="max_context_tokens",
        ),
        sa.CheckConstraint(
            "rate_limit_rpm IS NULL OR rate_limit_rpm >= 1",
            name="rate_limit_rpm",
        ),
        sa.CheckConstraint(
            "snapshot_hash ~ '^sha256:[a-f0-9]{64}$'",
            name="snapshot_hash",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_model_binding_snapshot__tenant_id__tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "model_config_definition_id"],
            ["resource_definition.tenant_id", "resource_definition.id"],
            name="fk_model_binding_snapshot__config_definition",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "model_config_version_id"],
            ["resource_version.tenant_id", "resource_version.id"],
            name="fk_model_binding_snapshot__config_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "provider_definition_id"],
            ["resource_definition.tenant_id", "resource_definition.id"],
            name="fk_model_binding_snapshot__provider_definition",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_model_binding_snapshot"),
        sa.UniqueConstraint(
            "model_config_version_id",
            name="uq_model_binding_snapshot__model_config_version_id",
        ),
    )
    op.create_index(
        "ix_model_binding_snapshot__tenant_model_config_definition",
        "model_binding_snapshot",
        ["tenant_id", "model_config_definition_id"],
        unique=False,
    )
    op.create_index(
        "ix_model_binding_snapshot__tenant_provider_definition",
        "model_binding_snapshot",
        ["tenant_id", "provider_definition_id"],
        unique=False,
    )

    # Historical Provider revisions do not exist, so legacy versions are
    # backfilled from the current Provider draft. New publications are frozen
    # atomically by SqlAlchemyResourceRegistry.
    op.execute(sa.text("""
            INSERT INTO model_binding_snapshot (
                tenant_id,
                model_config_definition_id,
                model_config_version_id,
                provider_definition_id,
                provider_type,
                base_url,
                secret_ref,
                provider_timeout_seconds,
                model_id,
                capabilities_json,
                default_parameters_json,
                max_context_tokens,
                rate_limit_rpm,
                snapshot_hash,
                created_at
            )
            SELECT
                version.tenant_id,
                version.definition_id,
                version.id,
                (version.content_json ->> 'provider_id')::uuid,
                provider.current_draft_json ->> 'provider_type',
                provider.current_draft_json ->> 'base_url',
                provider.current_draft_json ->> 'secret_ref',
                (provider.current_draft_json ->> 'timeout_seconds')::integer,
                version.content_json ->> 'model_id',
                version.content_json -> 'capabilities',
                version.content_json -> 'default_parameters',
                (version.content_json ->> 'max_context_tokens')::bigint,
                (version.content_json ->> 'rate_limit_rpm')::integer,
                'sha256:' || encode(
                    digest(
                        convert_to(
                            jsonb_build_object(
                                'provider_id', version.content_json ->> 'provider_id',
                                'provider_type', provider.current_draft_json ->> 'provider_type',
                                'base_url', provider.current_draft_json ->> 'base_url',
                                'secret_ref', provider.current_draft_json ->> 'secret_ref',
                                'provider_timeout_seconds', provider.current_draft_json ->> 'timeout_seconds',
                                'model_id', version.content_json ->> 'model_id',
                                'capabilities', version.content_json -> 'capabilities',
                                'default_parameters', version.content_json -> 'default_parameters',
                                'max_context_tokens', version.content_json -> 'max_context_tokens',
                                'rate_limit_rpm', version.content_json -> 'rate_limit_rpm'
                            )::text,
                            'UTF8'
                        ),
                        'sha256'
                    ),
                    'hex'
                ),
                version.published_at
            FROM resource_version AS version
            JOIN resource_definition AS model_config
              ON model_config.tenant_id = version.tenant_id
             AND model_config.id = version.definition_id
             AND model_config.resource_type = 'model_config'
            JOIN resource_definition AS provider
              ON provider.tenant_id = version.tenant_id
             AND provider.id = (version.content_json ->> 'provider_id')::uuid
             AND provider.resource_type = 'model_provider'
            """))

    op.execute(sa.text("ALTER TABLE model_binding_snapshot ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE model_binding_snapshot FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON model_binding_snapshot "
            f"USING ({TENANT_POLICY_EXPRESSION}) "
            f"WITH CHECK ({TENANT_POLICY_EXPRESSION})"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("DROP POLICY IF EXISTS tenant_isolation ON model_binding_snapshot")
    )
    op.execute(sa.text("ALTER TABLE model_binding_snapshot DISABLE ROW LEVEL SECURITY"))
    op.drop_index(
        "ix_model_binding_snapshot__tenant_provider_definition",
        table_name="model_binding_snapshot",
    )
    op.drop_index(
        "ix_model_binding_snapshot__tenant_model_config_definition",
        table_name="model_binding_snapshot",
    )
    op.drop_table("model_binding_snapshot")
    op.drop_constraint(
        "uq_resource_version__tenant_id_id",
        "resource_version",
        type_="unique",
    )

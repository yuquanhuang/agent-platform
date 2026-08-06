"""Add Prompt tenant-admin permissions and explicit rollback versions.

Revision ID: 0005_prompt_permissions
Revises: 0004_resource_registry
Create Date: 2026-08-06
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0005_prompt_permissions"
down_revision: str | None = "0004_resource_registry"
branch_labels: str | None = None
depends_on: str | None = None

PROMPT_ACTIONS = (
    "create",
    "read",
    "list",
    "update",
    "delete",
    "publish",
    "rollback",
    "disable",
)


def upgrade() -> None:
    op.add_column(
        "resource_version",
        sa.Column(
            "publication_kind",
            sa.String(length=20),
            server_default=sa.text("'PUBLISH'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_resource_version__publication_kind",
        "resource_version",
        "publication_kind IN ('PUBLISH', 'ROLLBACK')",
    )
    op.drop_constraint(
        "uq_resource_version__definition_id_content_hash",
        "resource_version",
        type_="unique",
    )
    op.create_index(
        "uq_resource_version__definition_content_publish",
        "resource_version",
        ["definition_id", "content_hash"],
        unique=True,
        postgresql_where=sa.text("publication_kind = 'PUBLISH'"),
    )
    for action in PROMPT_ACTIONS:
        op.execute(sa.text(f"""DO $$ DECLARE tenant_uuid uuid; BEGIN
                FOR tenant_uuid IN SELECT tenant_id FROM role
                WHERE built_in IS TRUE AND code = 'tenant_admin' LOOP
                    PERFORM set_config(
                        'app.current_tenant_id', tenant_uuid::text, true
                    );
                    INSERT INTO role_permission
                        (tenant_id, role_id, resource_type, action)
                    SELECT tenant_id, id, 'prompt', '{action}' FROM role
                    WHERE tenant_id = tenant_uuid AND built_in IS TRUE
                    AND code = 'tenant_admin' ON CONFLICT DO NOTHING;
                END LOOP; END $$"""))


def downgrade() -> None:
    for action in PROMPT_ACTIONS:
        op.execute(sa.text(f"""DO $$ DECLARE tenant_uuid uuid; BEGIN
                FOR tenant_uuid IN SELECT tenant_id FROM role
                WHERE built_in IS TRUE AND code = 'tenant_admin' LOOP
                    PERFORM set_config(
                        'app.current_tenant_id', tenant_uuid::text, true
                    );
                    DELETE FROM role_permission WHERE tenant_id = tenant_uuid
                    AND resource_type = 'prompt' AND action = '{action}';
                END LOOP; END $$"""))
    op.drop_index(
        "uq_resource_version__definition_content_publish",
        table_name="resource_version",
    )
    # The AP-E1-001 schema cannot represent duplicate historical content.
    # Downgrading therefore removes versions whose only purpose is rollback history.
    op.execute(
        sa.text("DELETE FROM resource_version WHERE publication_kind = 'ROLLBACK'")
    )
    op.create_unique_constraint(
        "uq_resource_version__definition_id_content_hash",
        "resource_version",
        ["definition_id", "content_hash"],
    )
    op.drop_constraint(
        "ck_resource_version__publication_kind",
        "resource_version",
        type_="check",
    )
    op.drop_column("resource_version", "publication_kind")

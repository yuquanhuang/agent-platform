"""Add Model Provider and Model Config tenant-admin permissions.

Revision ID: 0006_model_permissions
Revises: 0005_prompt_permissions
Create Date: 2026-08-07
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0006_model_permissions"
down_revision: str | None = "0005_prompt_permissions"
branch_labels: str | None = None
depends_on: str | None = None

MODEL_PERMISSIONS = {
    "model_provider": (
        "create",
        "read",
        "list",
        "update",
        "delete",
        "disable",
        "execute",
    ),
    "model_config": (
        "create",
        "read",
        "list",
        "update",
        "delete",
        "publish",
        "rollback",
        "disable",
    ),
}


def upgrade() -> None:
    for resource_type, actions in MODEL_PERMISSIONS.items():
        for action in actions:
            op.execute(sa.text(f"""DO $$ DECLARE tenant_uuid uuid; BEGIN
                    FOR tenant_uuid IN SELECT tenant_id FROM role
                    WHERE built_in IS TRUE AND code = 'tenant_admin' LOOP
                        PERFORM set_config(
                            'app.current_tenant_id', tenant_uuid::text, true
                        );
                        INSERT INTO role_permission
                            (tenant_id, role_id, resource_type, action)
                        SELECT tenant_id, id, '{resource_type}', '{action}' FROM role
                        WHERE tenant_id = tenant_uuid AND built_in IS TRUE
                        AND code = 'tenant_admin' ON CONFLICT DO NOTHING;
                    END LOOP; END $$"""))


def downgrade() -> None:
    for resource_type, actions in MODEL_PERMISSIONS.items():
        for action in actions:
            op.execute(sa.text(f"""DO $$ DECLARE tenant_uuid uuid; BEGIN
                    FOR tenant_uuid IN SELECT tenant_id FROM role
                    WHERE built_in IS TRUE AND code = 'tenant_admin' LOOP
                        PERFORM set_config(
                            'app.current_tenant_id', tenant_uuid::text, true
                        );
                        DELETE FROM role_permission WHERE tenant_id = tenant_uuid
                        AND resource_type = '{resource_type}' AND action = '{action}';
                    END LOOP; END $$"""))

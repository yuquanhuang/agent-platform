"""Backfill tenant administrator Run cancel and retry permissions.

Revision ID: 0018_run_control_permissions
Revises: 0017_agent_run
Create Date: 2026-08-08
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0018_run_control_permissions"
down_revision: str | None = "0017_agent_run"
branch_labels: str | None = None
depends_on: str | None = None

RUN_CONTROL_PERMISSIONS = ("cancel", "retry")


def upgrade() -> None:
    for action in RUN_CONTROL_PERMISSIONS:
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
    for action in RUN_CONTROL_PERMISSIONS:
        op.execute(sa.text(f"""DO $$ DECLARE tenant_uuid uuid; BEGIN
                FOR tenant_uuid IN SELECT tenant_id FROM role
                WHERE built_in IS TRUE AND code = 'tenant_admin' LOOP
                    PERFORM set_config(
                        'app.current_tenant_id', tenant_uuid::text, true
                    );
                    DELETE FROM role_permission WHERE tenant_id = tenant_uuid
                    AND resource_type = 'run' AND action = '{action}';
                END LOOP; END $$"""))

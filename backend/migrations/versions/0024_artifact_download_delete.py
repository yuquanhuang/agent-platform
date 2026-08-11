"""Enable Artifact download, expiry and asynchronous deletion.

Revision ID: 0024_artifact_download_delete
Revises: 0023_artifact_upload_scan
Create Date: 2026-08-09
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0024_artifact_download_delete"
down_revision: str | None = "0023_artifact_upload_scan"
branch_labels: str | None = None
depends_on: str | None = None

ARTIFACT_LIFECYCLE_PERMISSIONS = ("download", "delete")


def upgrade() -> None:
    op.drop_constraint("trusted_object_status", "artifact", type_="check")
    op.create_check_constraint(
        "trusted_object_status",
        "artifact",
        "(status IN ('AVAILABLE','EXPIRED') AND object_uri IS NOT NULL) OR "
        "(status IN ('UPLOADING','SCANNING','REJECTED','FAILED') "
        "AND object_uri IS NULL) OR status IN ('DELETING','DELETED')",
    )
    for action in ARTIFACT_LIFECYCLE_PERMISSIONS:
        op.execute(sa.text(f"""DO $$ DECLARE tenant_uuid uuid; BEGIN
                FOR tenant_uuid IN SELECT tenant_id FROM role
                WHERE built_in IS TRUE AND code = 'tenant_admin' LOOP
                    PERFORM set_config(
                        'app.current_tenant_id', tenant_uuid::text, true
                    );
                    INSERT INTO role_permission
                        (tenant_id, role_id, resource_type, action)
                    SELECT tenant_id, id, 'artifact', '{action}' FROM role
                    WHERE tenant_id = tenant_uuid AND built_in IS TRUE
                    AND code = 'tenant_admin' ON CONFLICT DO NOTHING;
                END LOOP; END $$"""))


def downgrade() -> None:
    for action in ARTIFACT_LIFECYCLE_PERMISSIONS:
        op.execute(sa.text(f"""DO $$ DECLARE tenant_uuid uuid; BEGIN
                FOR tenant_uuid IN SELECT tenant_id FROM role
                WHERE built_in IS TRUE AND code = 'tenant_admin' LOOP
                    PERFORM set_config(
                        'app.current_tenant_id', tenant_uuid::text, true
                    );
                    DELETE FROM role_permission WHERE tenant_id = tenant_uuid
                    AND resource_type = 'artifact' AND action = '{action}';
                END LOOP; END $$"""))
    op.execute(
        sa.text(
            "UPDATE artifact SET object_uri = "
            "'artifact://tenant/' || tenant_id::text || '/artifact/' || id::text "
            "WHERE status IN ('DELETING','DELETED') AND object_uri IS NULL"
        )
    )
    op.drop_constraint("trusted_object_status", "artifact", type_="check")
    op.create_check_constraint(
        "trusted_object_status",
        "artifact",
        "(status IN ('AVAILABLE','EXPIRED','DELETING','DELETED') "
        "AND object_uri IS NOT NULL) OR "
        "(status IN ('UPLOADING','SCANNING','REJECTED','FAILED') "
        "AND object_uri IS NULL)",
    )

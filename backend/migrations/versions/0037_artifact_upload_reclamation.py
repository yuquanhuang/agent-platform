"""Index expired Artifact upload reclamation sweeps.

Revision ID: 0037_artifact_upload_reclamation
Revises: 0036_model_usage_cost_provenance
Create Date: 2026-08-12
"""

from alembic import op

revision: str = "0037_artifact_upload_reclamation"
down_revision: str | None = "0036_model_usage_cost_provenance"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "ix_artifact__tenant_status_upload_expires_at",
        "artifact",
        ["tenant_id", "status", "upload_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_artifact__tenant_status_upload_expires_at", table_name="artifact")

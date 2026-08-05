"""Offline Alembic SQL verification for the IAM foundation migration."""

from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config

BACKEND_ROOT = Path(__file__).resolve().parents[3]


def render_upgrade_sql() -> str:
    output = StringIO()
    config = Config(str(BACKEND_ROOT / "alembic.ini"), output_buffer=output)
    config.attributes["database_url"] = (
        "postgresql+asyncpg://migration:placeholder@localhost/agent_platform"
    )

    command.upgrade(config, "head", sql=True)

    return output.getvalue()


def test_offline_upgrade_contains_foundation_tables_and_extensions() -> None:
    sql = render_upgrade_sql()

    assert "CREATE EXTENSION IF NOT EXISTS pgcrypto" in sql
    assert "CREATE EXTENSION IF NOT EXISTS citext" in sql
    for table_name in ("tenant", "app_user", "tenant_member", "role", "role_binding"):
        assert f"CREATE TABLE {table_name}" in sql


def test_offline_upgrade_contains_membership_and_cross_tenant_constraints() -> None:
    sql = render_upgrade_sql()

    assert "membership_version BIGINT DEFAULT 1 NOT NULL" in sql
    assert "ck_tenant_member__membership_version" in sql
    assert "uq_tenant_member__tenant_id_user_id" in sql
    assert "fk_role_binding__tenant_id_role_id__role" in sql


def test_offline_upgrade_enables_and_forces_rls_with_write_checks() -> None:
    sql = render_upgrade_sql()

    for table_name in ("tenant_member", "role", "role_binding"):
        assert f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY" in sql
        assert f"CREATE POLICY tenant_isolation ON {table_name}" in sql

    assert sql.count("WITH CHECK") == 3
    assert sql.count("current_setting('app.current_tenant_id', true)") == 6

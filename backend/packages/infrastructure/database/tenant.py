"""Bind the authoritative TenantContext to a PostgreSQL transaction."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.contracts.public import TenantContext

TENANT_SETTING_NAME = "app.current_tenant_id"


async def bind_tenant_context(
    session: AsyncSession, tenant_context: TenantContext
) -> None:
    """Set a transaction-local tenant identifier consumed by RLS policies."""

    if not session.in_transaction():
        raise RuntimeError("tenant context must be bound inside an active transaction")

    await session.execute(
        text("SELECT set_config(:setting_name, :tenant_id, true)"),
        {
            "setting_name": TENANT_SETTING_NAME,
            "tenant_id": tenant_context.tenant_id,
        },
    )
    session.info["tenant_context"] = tenant_context

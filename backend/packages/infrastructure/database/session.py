"""Async PostgreSQL engine and session construction."""

from pydantic import SecretStr
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_database_engine(
    resolved_database_dsn: SecretStr,
    *,
    service_name: str,
    statement_timeout_ms: int = 30_000,
) -> AsyncEngine:
    """Create an asyncpg engine from a DSN resolved outside process settings."""

    if not service_name.strip():
        raise ValueError("service_name must not be empty")
    if statement_timeout_ms < 1:
        raise ValueError("statement_timeout_ms must be positive")

    url = make_url(resolved_database_dsn.get_secret_value())
    if url.drivername != "postgresql+asyncpg":
        raise ValueError("database DSN must use postgresql+asyncpg")

    return create_async_engine(
        url,
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "application_name": service_name,
                "statement_timeout": str(statement_timeout_ms),
            }
        },
    )


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Create explicit, non-expiring sessions for requests and activities."""

    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )

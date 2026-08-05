# Database migrations

Migrations run as an independent deployment job. The API and workers never call
Alembic during startup.

The deployment composition layer must resolve `AP_DATABASE_DSN_REF` through the
configured Secret Backend and pass the resolved `postgresql+asyncpg://` URL as
`alembic.config.Config.attributes["database_url"]`. The URL is intentionally not
stored in `alembic.ini` or process settings.

Offline SQL generation uses the same attribute and does not connect to a
database. Production changes follow expand → backfill → contract; extensions
are retained during downgrade because they may be shared by later revisions.

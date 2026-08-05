# Agent Platform Backend

Python 3.12 backend for the Agent Platform. Application entrypoints live in
`apps/`; reusable code follows the dependency boundaries under `packages/`.

## Local API

```bash
cp .env.example .env
uv run python -m apps.api.main
```

The initial API skeleton exposes:

- `GET /health/live`: process liveness only.
- `GET /health/ready`: configuration and frozen-contract readiness.

Worker modules under `apps/` are reserved process boundaries. Until their
corresponding Epic tasks are implemented, invoking them exits with an explicit
non-zero error instead of reporting a false healthy process.

## Database migrations

PostgreSQL migrations live in `migrations/` and run as an independent deployment
job. API and worker startup never execute Alembic automatically. The composition
layer resolves `AP_DATABASE_DSN_REF` through the configured Secret Backend and
injects the resolved DSN into Alembic; no plaintext DSN is stored in repository
configuration.

Tenant-scoped application work uses `TenantUnitOfWork`, which creates one
`AsyncSession`, starts a transaction, and binds the authoritative
`TenantContext` before any repository query can run. PostgreSQL RLS is a defense
layer and does not replace explicit `tenant_id` repository predicates.

The real PostgreSQL integration test is opt-in and rejects database names that
do not end in `_test`:

```bash
AP_TEST_DATABASE_URL=postgresql+asyncpg://localhost/agent_platform_test \
  uv run pytest tests/integration/database/test_postgresql_rls.py
```

## Quality gates

From the repository root:

```bash
make backend-check
```

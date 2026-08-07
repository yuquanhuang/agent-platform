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

## Local/test Mock identity

When `AP_AUTH_MODE=mock`, the API accepts only the non-secret sentinel header
`Authorization: Bearer mock`. Subject, platform role, active tenant and
`membership_version` come exclusively from `AP_MOCK_*` server configuration;
client headers, query parameters and request bodies cannot override them.

`GET /api/v1/me` then loads the platform user and memberships from PostgreSQL.
If the configured membership version is stale, or the active membership is
disabled, the request fails closed. The identity reader requires a separately
injected platform/bootstrap Session Factory; normal Member/Role operations must
continue to use tenant-bound sessions and must not reuse that privileged path.

## IAM management APIs

The frozen Tenant, Member, Role and Operation endpoints are implemented under
`/api/v1`. Tenant administration requires the server-derived `platform_admin`
role and uses the explicit platform transaction boundary. Member and Role
operations derive the active tenant from the authenticated principal, resolve
current permissions, then execute through `TenantUnitOfWork` with explicit
tenant predicates and RLS.

Create/action/delete requests persist a 24-hour idempotency fingerprint and
replayable response. Editable resources use the strong ETag format
`"rv:<resource_version>"`; member/role authorization changes increment affected
`membership_version` values in the same transaction. Delete endpoints currently
finish synchronously but return a contract-compatible Operation handle whose
query state is already `SUCCEEDED`.

The process entrypoint still does not resolve Secret References itself. The
deployment composition layer must construct `SqlAlchemyIamPersistence`, wrap it
with `IamManagementService`, and inject it into `create_app`; without that
explicit composition the IAM routes fail closed with `DEPENDENCY_UNAVAILABLE`.

Release, Deployment, version history, Snapshot Diff and read-only publication
preview have a production composition boundary in `apps.api.composition`. The
deployment layer resolves `AP_DATABASE_DSN_REF`, builds the async Session Factory,
loads trusted Runtime Target configuration, then calls
`create_database_publication_app`; the default import-time app remains
fail-closed and never guesses Secret or Runtime Target configuration.

## Temporal, Outbox and observability foundation

`temporal-worker-control` and `temporal-worker-run` are now real independent
Temporal worker entrypoints. They register the same versioned connectivity
Probe Workflow/Activity on the fixed `control-plane` and `run-orchestrator`
queues, respectively. Worker startup requires `AP_TEMPORAL_ADDRESS`; the
namespace defaults to `agent-platform-<environment>`, connection retry is
bounded, and missing Temporal never reports readiness.

Migration `0003_temporal_outbox` adds the tenant-scoped `outbox_event` table,
RLS and the `status + next_attempt_at` claim index. Business use cases add rows
through `SqlAlchemyOutboxWriter` using their existing `AsyncSession`, so the
business fact and Outbox row commit atomically. `SqlAlchemyOutboxStore` claims
and records delivery outcomes in separate short transactions; Temporal RPC is
performed by `OutboxDispatcher` only after claim commit. The event-worker loop
accepts an explicit bounded `TenantContextSource`; production tenant enumeration
and Secret Reference resolution remain deployment composition responsibilities.

Backend entrypoints share JSON logging, OpenTelemetry setup and low-cardinality
Prometheus metrics. The API exposes `/metrics` only to
`AP_METRICS_ALLOWED_NETWORKS`, which defaults to loopback networks for local
development. Tenant, Run and Workflow identifiers are not used as Prometheus
labels.

## Quality gates

From the repository root:

```bash
make backend-check
```

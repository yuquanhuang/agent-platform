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

Event and Reconciliation polling loops share a bounded recovery policy for
explicit database, Redis and Temporal transport failures. Backoff is
interruptible during shutdown, a successful cycle resets the failure count,
and persistent dependency failure exits non-zero so the deployment orchestrator
can replace the process. Unknown application errors are never treated as
transient. Per-event Outbox retry and fencing remain responsible for preventing
unsafe external side-effect replay.

Deployment composition uses `SqlAlchemyTenantContextSource` to rotate over
operational ACTIVE/DISABLED tenants with an explicit service subject, then
combines a fixed set of isolated Event dispatchers or builds the database-backed
Platform Reconciler.
The process wrappers install SIGINT/SIGTERM handlers, expose `process_up`, and
drain the current bounded cycle before stopping. `event-worker/main.py` now
resolves Env Secret references and composes PostgreSQL, Temporal, Redis and
MinIO production adapters. `reconciliation-worker/main.py` now composes
PostgreSQL, Temporal, tenant enumeration and an authenticated HTTP Sandbox
cleanup controller. Each cleanup request uses an Ed25519-signed token with a
bounded lifetime, `aud=sandbox-manager`, service subject, tenant, permission and
`jti`; tenant headers are never trusted. The worker also requires a separate
Execution Ticket HMAC key so Approval repair does not silently remain partial.

## Secret Backend and Kubernetes

`EnvSecretBackend` accepts only `secret://env/AP_SECRET_*` references. This is
the initial Kubernetes mode: Secret values are injected with `secretKeyRef`,
while application configuration contains references rather than plaintext.
Missing, empty, oversized or malformed values fail closed and resolved values
remain `SecretStr`. `AP_SECRET_BACKEND=vault` is reserved and intentionally
fails startup until a reviewed Vault adapter is configured.

The Kubernetes base under `infra/kubernetes/agent-platform` enables the
production-composed Event Worker. A controlled Reconciliation Worker manifest
is provided separately and is enabled only after the Sandbox Manager endpoint,
stable service subject and signing key have been installed. The API, Sandbox
Manager and AgentScope Runtime Worker are not declared ready merely by creating
Deployments; their remaining Provider/runtime composition boundaries are
recorded in the Kubernetes README. Existing CI builds the controlled backend
Dockerfile and must replace the development image tag with a verified Registry
digest.

## Artifact Download Gateway

Artifact download authorization returns a short-lived platform Gateway URL,
not an S3/MinIO presigned URL. The opaque token is returned once and only its
SHA-256 hash is stored in `artifact_download_grant`. Each download resolves the
grant through the explicit platform transaction boundary, then validates the
Artifact and opens the private object with a deployment-injected SERVICE
`TenantContext`; Artifact deletion revokes all grants before publishing object
cleanup work.

Production must inject the private object reader and a stable Gateway service
subject. The MinIO adapter covers upload, inspection, bounded scan copy,
promotion, delete and trusted full/Range reads with timeouts and bounded
concurrency. Full responses use 200, valid single byte ranges use 206, and
invalid/multiple/unsatisfied ranges use the frozen 416 response.

Open streams subscribe to Redis artifact-revocation wake-ups and periodically
recheck PostgreSQL Grant/Artifact facts; Redis remains non-authoritative and a
notification outage degrades to database polling. Downloads also have a
configured maximum duration. Application exception logs record only the path,
not the query token. Production uses a dedicated Ingress with query-free access
logging (or access logging disabled) and proxy-level bandwidth shaping.

## Run capacity admission

Run creation and retry can enforce hard non-terminal concurrency limits for the
tenant, user, Agent and Runtime type. The PostgreSQL store obtains sorted
transaction advisory locks before counting and inserting, so concurrent API
instances cannot over-admit the same capacity slot. A rejected request returns
`429 RATE_LIMITED` and writes a DENIED Audit record; its idempotency claim is
rolled back.

Staging and production API composition fail closed unless all five limits are
configured: `AP_RUN_MAX_NONTERMINAL_PER_TENANT`,
`AP_RUN_MAX_NONTERMINAL_PER_USER`, `AP_RUN_MAX_NONTERMINAL_PER_AGENT`,
`AP_RUN_MAX_NONTERMINAL_AGENTSCOPE` and `AP_RUN_MAX_NONTERMINAL_CODEX`.

When Artifact storage is enabled, staging and production must also configure
`AP_ARTIFACT_MAX_RESERVED_BYTES_PER_TENANT` and
`AP_ARTIFACT_MAX_RESERVED_COUNT_PER_TENANT`. Upload reservations that would
exceed either tenant hard limit return the existing `RATE_LIMITED` error before
an object-store grant is issued. Artifacts continue to reserve capacity until
physical deletion reaches `DELETED`.

Tenant administrators can manage one durable StoragePolicy with separate
Workspace byte/count and Artifact byte/count pools. ACTIVE limits only narrow
the deployment hard limits; DISABLED or absent policies fall back to deployment
configuration. Sandbox Manager defaults Workspace tenant reservations to 10 GiB
and 100 Workspaces through `AP_WORKSPACE_MAX_RESERVED_BYTES_PER_TENANT` and
`AP_WORKSPACE_MAX_RESERVED_COUNT_PER_TENANT`. Existing Run Workspace quotas stay
frozen; only new Workspace admission observes the current policy version.

Artifact retention is frozen per lifecycle fact. `AVAILABLE` Artifacts default
to 30 days and `FAILED`/`REJECTED`/`EXPIRED` Artifacts default to a seven-day
forensic window. Active legal holds are keyed by case reference and block both
automatic and manual deletion. The API and Event Worker must receive identical
values for `AP_ARTIFACT_RETENTION_SECONDS`,
`AP_ARTIFACT_FORENSIC_RETENTION_SECONDS`,
`AP_ARTIFACT_DELETE_RECOVERY_DELAY_SECONDS` and
`AP_ARTIFACT_DELETE_RECOVERY_MAX_OPERATIONS`; existing deadlines never drift
when configuration changes.

The Reconciliation Worker owns global capacity admission by Deployment
`runtime_target_id`. `AP_RUN_CAPACITY_DOMAIN_SLOTS` is a required JSON mapping
for staging/production reconciliation, while lease TTL and cross-tenant quantum
default to 300 seconds and 1. A live Run's expired lease is renewed or fails
closed; only terminal or orphaned leases are released. Redis is not the lease
fact source.

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

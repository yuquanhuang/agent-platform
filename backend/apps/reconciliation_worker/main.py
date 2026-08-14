"""Kubernetes-ready Reconciliation Worker entrypoint."""

import asyncio

import httpx
from prometheus_client import start_http_server

from apps.reconciliation_worker.composition import build_database_platform_reconciler
from apps.reconciliation_worker.runner import run_reconciliation_worker_process
from packages.application.policy import RunCapacityPolicy
from packages.infrastructure.public import (
    AppSettings,
    Ed25519ServiceTokenIssuer,
    HmacExecutionTicketIssuer,
    HttpSandboxCleanupController,
    PlatformMetrics,
    SqlAlchemyTenantContextSource,
    build_secret_backend,
    configure_json_logging,
    configure_tracing,
    connect_temporal_client,
    create_database_engine,
    create_session_factory,
    get_settings,
)


async def run(settings: AppSettings) -> None:
    missing = [
        name
        for name, value in {
            "AP_DATABASE_DSN_REF": settings.database_dsn_ref,
            "AP_SERVICE_SUBJECT_ID": settings.service_subject_id,
            "AP_SANDBOX_MANAGER_BASE_URL": settings.sandbox_manager_base_url,
            "AP_INTERNAL_SERVICE_TOKEN_ISSUER": settings.internal_service_token_issuer,
            "AP_INTERNAL_SERVICE_TOKEN_SIGNING_KEY_REF": (
                settings.internal_service_token_signing_key_ref
            ),
            "AP_EXECUTION_TICKET_KEY_REF": settings.execution_ticket_key_ref,
        }.items()
        if value is None
    ]
    if missing:
        raise RuntimeError("Reconciliation Worker requires: " + ", ".join(missing))
    if settings.env.value in {"staging", "production"}:
        capacity_values = {
            "AP_RUN_MAX_NONTERMINAL_PER_TENANT": (
                settings.run_max_nonterminal_per_tenant
            ),
            "AP_RUN_MAX_NONTERMINAL_PER_USER": settings.run_max_nonterminal_per_user,
            "AP_RUN_MAX_NONTERMINAL_PER_AGENT": (
                settings.run_max_nonterminal_per_agent
            ),
            "AP_RUN_MAX_NONTERMINAL_AGENTSCOPE": (
                settings.run_max_nonterminal_agentscope
            ),
            "AP_RUN_MAX_NONTERMINAL_CODEX": settings.run_max_nonterminal_codex,
            "AP_RUN_CAPACITY_DOMAIN_SLOTS": (
                settings.run_capacity_domain_slots or None
            ),
        }
        missing_capacity = [
            name for name, value in capacity_values.items() if value is None
        ]
        if missing_capacity:
            raise RuntimeError(
                "Reconciliation Worker Run capacity requires: "
                + ", ".join(missing_capacity)
            )

    assert settings.database_dsn_ref is not None
    assert settings.service_subject_id is not None
    assert settings.sandbox_manager_base_url is not None
    assert settings.internal_service_token_issuer is not None
    assert settings.internal_service_token_signing_key_ref is not None
    assert settings.execution_ticket_key_ref is not None

    secrets = build_secret_backend(settings.secret_backend)
    database_dsn = await secrets.resolve(settings.database_dsn_ref)
    signing_key = await secrets.resolve(settings.internal_service_token_signing_key_ref)
    execution_ticket_key = await secrets.resolve(settings.execution_ticket_key_ref)
    engine = create_database_engine(
        database_dsn,
        service_name=settings.service_name,
    )
    try:
        session_factory = create_session_factory(engine)
        temporal_client = await connect_temporal_client(settings)
        token_issuer = Ed25519ServiceTokenIssuer(
            issuer=str(settings.internal_service_token_issuer),
            audience=settings.internal_service_token_audience,
            subject_id=settings.service_subject_id,
            key_id=settings.internal_service_token_key_id,
            private_key=signing_key,
            ttl_seconds=settings.internal_service_token_ttl_seconds,
        )
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings.sandbox_manager_request_timeout_seconds),
            follow_redirects=False,
        ) as client:
            reconciler = build_database_platform_reconciler(
                session_factory,
                temporal_client=temporal_client,
                sandbox_cleanup=HttpSandboxCleanupController(
                    client=client,
                    base_url=str(settings.sandbox_manager_base_url),
                    token_issuer=token_issuer,
                ),
                ticket_issuer=HmacExecutionTicketIssuer(execution_ticket_key),
                run_capacity_policy=RunCapacityPolicy(
                    max_nonterminal_runs_per_tenant=(
                        settings.run_max_nonterminal_per_tenant
                    ),
                    max_nonterminal_runs_per_user=settings.run_max_nonterminal_per_user,
                    max_nonterminal_runs_per_agent=(
                        settings.run_max_nonterminal_per_agent
                    ),
                    max_nonterminal_agentscope_runs=(
                        settings.run_max_nonterminal_agentscope
                    ),
                    max_nonterminal_codex_runs=settings.run_max_nonterminal_codex,
                ),
                run_queue_batch_size=settings.run_queue_admission_batch_size,
                run_queue_max_wait_seconds=settings.run_queue_max_wait_seconds,
                run_queue_max_pending_per_tenant=(
                    settings.run_queue_max_pending_per_tenant
                ),
                run_capacity_domain_slots=settings.run_capacity_domain_slots,
                run_capacity_lease_ttl_seconds=(
                    settings.run_capacity_lease_ttl_seconds
                ),
                run_capacity_tenant_quantum=settings.run_capacity_tenant_quantum,
            )
            contexts = SqlAlchemyTenantContextSource(
                session_factory,
                service_subject_id=settings.service_subject_id,
                process_name=settings.service_name,
            )
            metrics = PlatformMetrics()
            start_http_server(
                settings.worker_metrics_port,
                addr=settings.worker_metrics_host,
                registry=metrics.registry,
            )
            await run_reconciliation_worker_process(
                reconciler,
                contexts,
                metrics,
                tenant_limit=settings.worker_tenant_limit,
                poll_interval_seconds=(
                    settings.reconciliation_worker_poll_interval_seconds
                ),
                run_queue_poll_interval_seconds=(
                    settings.run_queue_poll_interval_seconds
                ),
            )
    finally:
        await engine.dispose()


def main() -> None:
    settings = get_settings()
    configure_json_logging(
        service_name=settings.service_name,
        environment=settings.env.value,
        level=settings.log_level.value,
    )
    configure_tracing(
        service_name=settings.service_name,
        environment=settings.env.value,
        endpoint=(
            str(settings.otel_exporter_otlp_endpoint)
            if settings.otel_exporter_otlp_endpoint is not None
            else None
        ),
    )
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()

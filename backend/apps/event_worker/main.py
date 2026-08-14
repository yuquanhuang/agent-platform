"""Kubernetes-ready Event Worker production entrypoint."""

import asyncio
from typing import cast

from prometheus_client import start_http_server

from apps.event_worker.composition import (
    build_artifact_download_revocation_dispatcher,
    build_artifact_lifecycle_dispatcher,
    build_artifact_scan_dispatcher,
    build_run_event_notification_dispatcher,
    build_temporal_outbox_dispatcher,
)
from apps.event_worker.runner import (
    CompositeTenantOutboxDispatcher,
    run_event_worker_process,
)
from packages.application.artifacts import (
    ArtifactDeletionObjectStore,
    ArtifactQuarantineContentReader,
    ArtifactTrustedObjectPublisher,
)
from packages.infrastructure.observability import PlatformMetrics
from packages.infrastructure.public import (
    AppSettings,
    MinioArtifactObjectStore,
    ObjectIntegrityArtifactSecurityScanner,
    SqlAlchemyTenantContextSource,
    build_secret_backend,
    configure_json_logging,
    configure_tracing,
    connect_temporal_client,
    create_database_engine,
    create_redis_client,
    create_session_factory,
    get_settings,
)
from packages.infrastructure.redis.events import AsyncRedisClient


async def run(settings: AppSettings) -> None:
    if settings.database_dsn_ref is None:
        raise RuntimeError("AP_DATABASE_DSN_REF is required for event-worker")
    if settings.redis_dsn_ref is None:
        raise RuntimeError("AP_REDIS_DSN_REF is required for event-worker")
    if (
        settings.object_storage_endpoint is None
        or settings.object_storage_bucket is None
        or settings.object_storage_credential_ref is None
    ):
        raise RuntimeError(
            "AP_OBJECT_STORAGE_ENDPOINT, AP_OBJECT_STORAGE_BUCKET and "
            "AP_OBJECT_STORAGE_CREDENTIAL_REF are required for event-worker"
        )
    if settings.service_subject_id is None:
        raise RuntimeError("AP_SERVICE_SUBJECT_ID is required for event-worker")

    secrets = build_secret_backend(settings.secret_backend)
    database_dsn = await secrets.resolve(settings.database_dsn_ref)
    redis_dsn = await secrets.resolve(settings.redis_dsn_ref)
    object_credentials = await secrets.resolve(settings.object_storage_credential_ref)
    engine = create_database_engine(
        database_dsn,
        service_name=settings.service_name,
    )
    redis = create_redis_client(redis_dsn)
    try:
        await redis.ping()
        session_factory = create_session_factory(engine)
        temporal_client = await connect_temporal_client(settings)
        metrics = PlatformMetrics()
        start_http_server(
            settings.worker_metrics_port,
            addr=settings.worker_metrics_host,
            registry=metrics.registry,
        )
        objects = MinioArtifactObjectStore.from_secret(
            endpoint_url=str(settings.object_storage_endpoint),
            bucket=settings.object_storage_bucket,
            credential=object_credentials,
            region=settings.object_storage_region,
            connect_timeout_seconds=(settings.object_storage_connect_timeout_seconds),
            read_timeout_seconds=settings.object_storage_read_timeout_seconds,
            download_chunk_bytes=settings.object_storage_download_chunk_bytes,
            max_concurrent_downloads=(settings.object_storage_max_concurrent_downloads),
            download_acquire_timeout_seconds=(
                settings.object_storage_download_acquire_timeout_seconds
            ),
        )
        await objects.check_ready()
        dispatchers = CompositeTenantOutboxDispatcher(
            [
                build_temporal_outbox_dispatcher(
                    settings,
                    session_factory=session_factory,
                    temporal_client=temporal_client,
                    metrics=metrics,
                ),
                build_run_event_notification_dispatcher(
                    settings,
                    session_factory=session_factory,
                    redis=cast(AsyncRedisClient, redis),
                ),
                build_artifact_download_revocation_dispatcher(
                    settings,
                    session_factory=session_factory,
                    redis=cast(AsyncRedisClient, redis),
                ),
                build_artifact_scan_dispatcher(
                    settings,
                    session_factory=session_factory,
                    scanner=ObjectIntegrityArtifactSecurityScanner(objects),
                    quarantine_reader=cast(ArtifactQuarantineContentReader, objects),
                    publisher=cast(ArtifactTrustedObjectPublisher, objects),
                ),
                build_artifact_lifecycle_dispatcher(
                    settings,
                    session_factory=session_factory,
                    objects=cast(ArtifactDeletionObjectStore, objects),
                ),
            ]
        )
        contexts = SqlAlchemyTenantContextSource(
            session_factory,
            service_subject_id=settings.service_subject_id,
            process_name=settings.service_name,
        )
        await run_event_worker_process(
            dispatchers,
            contexts,
            metrics,
            tenant_limit=settings.worker_tenant_limit,
            poll_interval_seconds=settings.event_worker_poll_interval_seconds,
        )
    finally:
        await redis.aclose()
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

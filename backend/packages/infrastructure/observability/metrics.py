"""Low-cardinality Prometheus metrics shared by API and workers."""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from packages.contracts.model_gateway import ModelUsage


class PlatformMetrics:
    """Own a registry so application factories and tests never duplicate metrics."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry(auto_describe=True)
        self.process_up = Gauge(
            "agent_platform_process_up",
            "Whether this backend process completed local initialization.",
            labelnames=("process",),
            registry=self.registry,
        )
        self.worker_cycles = Counter(
            "agent_platform_worker_cycles_total",
            "Bounded worker cycle and dependency recovery outcomes.",
            labelnames=("process", "outcome"),
            registry=self.registry,
        )
        self.worker_consecutive_failures = Gauge(
            "agent_platform_worker_consecutive_failures",
            "Current consecutive retryable dependency failures by worker process.",
            labelnames=("process",),
            registry=self.registry,
        )
        self.http_requests = Counter(
            "agent_platform_http_requests_total",
            "HTTP requests completed by status class.",
            labelnames=("method", "status_class"),
            registry=self.registry,
        )
        self.http_duration = Histogram(
            "agent_platform_http_request_duration_seconds",
            "HTTP request duration without high-cardinality route labels.",
            labelnames=("method",),
            registry=self.registry,
        )
        self.temporal_workflow_starts = Counter(
            "agent_platform_temporal_workflow_starts_total",
            "Temporal workflow start outcomes.",
            labelnames=("worker_kind", "outcome"),
            registry=self.registry,
        )
        self.outbox_dispatch = Counter(
            "agent_platform_outbox_dispatch_total",
            "Outbox delivery outcomes.",
            labelnames=("outcome",),
            registry=self.registry,
        )
        self.outbox_claimed = Gauge(
            "agent_platform_outbox_claimed",
            "Outbox messages claimed by the latest bounded poll.",
            registry=self.registry,
        )
        self.run_reconciliation = Counter(
            "agent_platform_run_reconciliation_total",
            "Stalled Run reconciliation outcomes.",
            labelnames=("outcome",),
            registry=self.registry,
        )
        self.approval_reconciliation = Counter(
            "agent_platform_approval_reconciliation_total",
            "Approval expiry, Ticket and Signal reconciliation outcomes.",
            labelnames=("outcome",),
            registry=self.registry,
        )
        self.sandbox_reconciliation = Counter(
            "agent_platform_sandbox_reconciliation_total",
            "Sandbox lifecycle reconciliation outcomes.",
            labelnames=("outcome",),
            registry=self.registry,
        )
        self.model_gateway_requests = Counter(
            "agent_platform_model_gateway_requests_total",
            "Normalized Model Gateway request outcomes.",
            labelnames=("provider", "mode", "outcome"),
            registry=self.registry,
        )
        self.model_gateway_fallbacks = Counter(
            "agent_platform_model_gateway_fallbacks_total",
            "Safe Model Gateway fallback transitions.",
            labelnames=("source_provider", "target_provider"),
            registry=self.registry,
        )
        self.model_gateway_tokens = Counter(
            "agent_platform_model_gateway_tokens_total",
            "Normalized model tokens by type and estimation status.",
            labelnames=("provider", "token_type", "estimated"),
            registry=self.registry,
        )

    def observe_http(self, *, method: str, status_code: int, duration: float) -> None:
        status_class = f"{status_code // 100}xx"
        self.http_requests.labels(method=method, status_class=status_class).inc()
        self.http_duration.labels(method=method).observe(duration)

    def observe_worker_cycle(self, *, process: str, outcome: str) -> None:
        self.worker_cycles.labels(process=process, outcome=outcome).inc()

    def set_worker_consecutive_failures(self, *, process: str, count: int) -> None:
        if count < 0:
            raise ValueError("worker consecutive failure count cannot be negative")
        self.worker_consecutive_failures.labels(process=process).set(count)

    def observe_outbox(
        self, *, claimed: int, published: int, retried: int, dead: int
    ) -> None:
        self.outbox_claimed.set(claimed)
        for outcome, count in (
            ("published", published),
            ("retried", retried),
            ("dead", dead),
        ):
            if count:
                self.outbox_dispatch.labels(outcome=outcome).inc(count)

    def observe_run_reconciliation(
        self,
        *,
        examined: int,
        mappings_recorded: int,
        requests_requeued: int,
        cancellations_signalled: int,
        unresolved: int,
    ) -> None:
        for outcome, count in (
            ("examined", examined),
            ("mapping_recorded", mappings_recorded),
            ("request_requeued", requests_requeued),
            ("cancellation_signalled", cancellations_signalled),
            ("unresolved", unresolved),
        ):
            if count:
                self.run_reconciliation.labels(outcome=outcome).inc(count)

    def observe_model_gateway_request(
        self, *, provider: str, mode: str, outcome: str
    ) -> None:
        self.model_gateway_requests.labels(
            provider=provider, mode=mode, outcome=outcome
        ).inc()

    def observe_approval_reconciliation(
        self,
        *,
        expired: int,
        tickets_repaired: int,
        signals_sent: int,
        unresolved: int,
    ) -> None:
        for outcome, count in (
            ("expired", expired),
            ("ticket_repaired", tickets_repaired),
            ("signal_sent", signals_sent),
            ("unresolved", unresolved),
        ):
            if count:
                self.approval_reconciliation.labels(outcome=outcome).inc(count)

    def observe_sandbox_reconciliation(
        self,
        *,
        examined: int,
        destroyed: int,
        quarantined: int,
        cleanup_failed: int,
        unresolved: int,
    ) -> None:
        for outcome, count in (
            ("examined", examined),
            ("destroyed", destroyed),
            ("quarantined", quarantined),
            ("cleanup_failed", cleanup_failed),
            ("unresolved", unresolved),
        ):
            if count:
                self.sandbox_reconciliation.labels(outcome=outcome).inc(count)

    def observe_model_gateway_fallback(
        self, *, source_provider: str, target_provider: str
    ) -> None:
        self.model_gateway_fallbacks.labels(
            source_provider=source_provider, target_provider=target_provider
        ).inc()

    def observe_model_gateway_usage(self, *, provider: str, usage: ModelUsage) -> None:
        for token_type, count in (
            ("input", usage.input_tokens),
            ("output", usage.output_tokens),
            ("reasoning", usage.reasoning_tokens),
            ("cache_read", usage.cache_read_tokens),
            ("cache_write", usage.cache_write_tokens),
        ):
            if count:
                self.model_gateway_tokens.labels(
                    provider=provider,
                    token_type=token_type,
                    estimated=str(usage.estimated).lower(),
                ).inc(count)

"""Low-cardinality Prometheus metrics shared by API and workers."""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


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

    def observe_http(self, *, method: str, status_code: int, duration: float) -> None:
        status_class = f"{status_code // 100}xx"
        self.http_requests.labels(method=method, status_class=status_class).inc()
        self.http_duration.labels(method=method).observe(duration)

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

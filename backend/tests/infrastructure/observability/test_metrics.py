"""Low-cardinality Model Gateway metric tests."""

from prometheus_client import generate_latest

from packages.contracts.model_gateway import ModelUsage
from packages.infrastructure.observability import PlatformMetrics


def test_model_gateway_metrics_exclude_tenant_run_and_model_labels() -> None:
    metrics = PlatformMetrics()
    metrics.observe_model_gateway_request(
        provider="openai", mode="generate", outcome="success"
    )
    metrics.observe_model_gateway_fallback(
        source_provider="openai", target_provider="qwen"
    )
    metrics.observe_model_gateway_usage(
        provider="qwen",
        usage=ModelUsage(
            input_tokens=3,
            output_tokens=2,
            reasoning_tokens=1,
            cache_read_tokens=0,
            cache_write_tokens=0,
            estimated=False,
            cost=None,
        ),
    )

    payload = generate_latest(metrics.registry).decode()

    assert 'provider="openai"' in payload
    assert 'target_provider="qwen"' in payload
    assert 'token_type="reasoning"' in payload
    assert "tenant_id" not in payload
    assert "run_id" not in payload
    assert "model=" not in payload


def test_run_reconciliation_metrics_use_only_bounded_outcomes() -> None:
    metrics = PlatformMetrics()
    metrics.observe_run_reconciliation(
        examined=3,
        mappings_recorded=1,
        requests_requeued=1,
        cancellations_signalled=1,
        unresolved=1,
    )

    payload = generate_latest(metrics.registry).decode()

    assert 'outcome="mapping_recorded"' in payload
    assert 'outcome="unresolved"' in payload
    assert "tenant_id" not in payload
    assert "run_id" not in payload
    assert "workflow_id" not in payload


def test_approval_and_sandbox_reconciliation_metrics_remain_low_cardinality() -> None:
    metrics = PlatformMetrics()
    metrics.observe_approval_reconciliation(
        expired=1,
        tickets_repaired=1,
        signals_sent=1,
        unresolved=1,
    )
    metrics.observe_sandbox_reconciliation(
        examined=2,
        destroyed=1,
        quarantined=0,
        cleanup_failed=1,
        unresolved=1,
    )

    payload = generate_latest(metrics.registry).decode()

    assert 'outcome="ticket_repaired"' in payload
    assert 'outcome="cleanup_failed"' in payload
    assert "sandbox_id" not in payload
    assert "approval_id" not in payload


def test_worker_recovery_metrics_remain_low_cardinality() -> None:
    metrics = PlatformMetrics()

    metrics.observe_worker_cycle(process="event-worker", outcome="recovered")
    metrics.set_worker_consecutive_failures(process="event-worker", count=2)

    payload = generate_latest(metrics.registry).decode()

    assert "agent_platform_worker_cycles_total" in payload
    assert 'process="event-worker"' in payload
    assert 'outcome="recovered"' in payload
    assert "agent_platform_worker_consecutive_failures" in payload
    assert "tenant_id" not in payload
    assert "run_id" not in payload

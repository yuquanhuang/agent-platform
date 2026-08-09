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

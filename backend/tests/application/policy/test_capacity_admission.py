"""Run capacity admission policy tests."""

import pytest

from packages.application.policy import (
    CapacityAdmissionDenied,
    RunCapacityFacts,
    RunCapacityPolicy,
    admit_run_capacity,
)


def test_capacity_admission_allows_counts_below_every_hard_limit() -> None:
    admit_run_capacity(
        RunCapacityPolicy(
            max_nonterminal_runs_per_tenant=10,
            max_nonterminal_runs_per_user=5,
            max_nonterminal_runs_per_agent=7,
            max_nonterminal_agentscope_runs=8,
            max_nonterminal_codex_runs=2,
        ),
        RunCapacityFacts(
            tenant_nonterminal_runs=9,
            user_nonterminal_runs=4,
            agent_nonterminal_runs=6,
            runtime_nonterminal_runs=7,
            runtime_type="agentscope",
        ),
    )


@pytest.mark.parametrize(
    ("capacity_facts", "scope", "reason_code"),
    [
        (
            RunCapacityFacts(
                tenant_nonterminal_runs=10,
                user_nonterminal_runs=0,
                agent_nonterminal_runs=0,
                runtime_nonterminal_runs=0,
                runtime_type="agentscope",
            ),
            "tenant",
            "RUN_TENANT_CONCURRENCY_LIMIT",
        ),
        (
            RunCapacityFacts(
                tenant_nonterminal_runs=0,
                user_nonterminal_runs=5,
                agent_nonterminal_runs=0,
                runtime_nonterminal_runs=0,
                runtime_type="agentscope",
            ),
            "user",
            "RUN_USER_CONCURRENCY_LIMIT",
        ),
        (
            RunCapacityFacts(
                tenant_nonterminal_runs=0,
                user_nonterminal_runs=0,
                agent_nonterminal_runs=7,
                runtime_nonterminal_runs=0,
                runtime_type="agentscope",
            ),
            "agent",
            "RUN_AGENT_CONCURRENCY_LIMIT",
        ),
        (
            RunCapacityFacts(
                tenant_nonterminal_runs=0,
                user_nonterminal_runs=0,
                agent_nonterminal_runs=0,
                runtime_nonterminal_runs=8,
                runtime_type="agentscope",
            ),
            "runtime",
            "RUN_RUNTIME_CONCURRENCY_LIMIT",
        ),
        (
            RunCapacityFacts(
                tenant_nonterminal_runs=0,
                user_nonterminal_runs=0,
                agent_nonterminal_runs=0,
                runtime_nonterminal_runs=2,
                runtime_type="codex",
            ),
            "runtime",
            "RUN_RUNTIME_CONCURRENCY_LIMIT",
        ),
    ],
)
def test_capacity_admission_returns_stable_scope_reason(
    capacity_facts: RunCapacityFacts, scope: str, reason_code: str
) -> None:
    policy = RunCapacityPolicy(
        max_nonterminal_runs_per_tenant=10,
        max_nonterminal_runs_per_user=5,
        max_nonterminal_runs_per_agent=7,
        max_nonterminal_agentscope_runs=8,
        max_nonterminal_codex_runs=2,
    )

    with pytest.raises(CapacityAdmissionDenied) as denied:
        admit_run_capacity(policy, capacity_facts)

    assert denied.value.scope == scope
    assert denied.value.reason_code == reason_code
    assert denied.value.current == denied.value.limit


def test_capacity_policy_rejects_zero_or_unbounded_configuration_values() -> None:
    with pytest.raises(ValueError, match="between 1 and 1000000"):
        RunCapacityPolicy(max_nonterminal_runs_per_tenant=0)
    with pytest.raises(ValueError, match="between 1 and 1000000"):
        RunCapacityPolicy(max_nonterminal_codex_runs=1_000_001)


def test_tenant_capacity_policy_can_only_narrow_deployment_hard_limits() -> None:
    deployment = RunCapacityPolicy(
        max_nonterminal_runs_per_tenant=100,
        max_nonterminal_runs_per_user=20,
        max_nonterminal_agentscope_runs=30,
    )
    tenant = RunCapacityPolicy(
        max_nonterminal_runs_per_tenant=40,
        max_nonterminal_runs_per_agent=5,
        max_nonterminal_agentscope_runs=50,
    )

    effective = deployment.narrowed_by(tenant)

    assert effective.max_nonterminal_runs_per_tenant == 40
    assert effective.max_nonterminal_runs_per_user == 20
    assert effective.max_nonterminal_runs_per_agent == 5
    assert effective.max_nonterminal_agentscope_runs == 30
    assert deployment.expansion_fields(tenant) == ("max_nonterminal_agentscope_runs",)

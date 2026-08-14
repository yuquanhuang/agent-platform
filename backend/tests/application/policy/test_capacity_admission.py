"""Run capacity admission policy tests."""

import pytest

from packages.application.policy import (
    ArtifactStorageAdmissionDenied,
    ArtifactStoragePolicy,
    CapacityAdmissionDenied,
    RunCapacityFacts,
    RunCapacityPolicy,
    TenantStoragePolicy,
    WorkspaceStorageAdmissionDenied,
    admit_artifact_storage,
    admit_run_capacity,
    admit_workspace_storage,
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


def test_artifact_storage_admission_accounts_for_reserved_bytes_and_count() -> None:
    policy = ArtifactStoragePolicy(
        max_reserved_bytes_per_tenant=100,
        max_reserved_artifacts_per_tenant=2,
    )

    admit_artifact_storage(
        policy,
        reserved_bytes=80,
        reserved_artifacts=1,
        requested_bytes=20,
    )

    with pytest.raises(ArtifactStorageAdmissionDenied) as bytes_denial:
        admit_artifact_storage(
            policy,
            reserved_bytes=80,
            reserved_artifacts=1,
            requested_bytes=21,
        )
    assert bytes_denial.value.reason_code == "ARTIFACT_TENANT_STORAGE_BYTES_LIMIT"
    assert bytes_denial.value.current == 80
    assert bytes_denial.value.requested == 21

    with pytest.raises(ArtifactStorageAdmissionDenied) as count_denial:
        admit_artifact_storage(
            policy,
            reserved_bytes=50,
            reserved_artifacts=2,
            requested_bytes=1,
        )
    assert count_denial.value.reason_code == "ARTIFACT_TENANT_STORAGE_COUNT_LIMIT"


def test_artifact_storage_policy_rejects_invalid_limits_and_facts() -> None:
    with pytest.raises(ValueError):
        ArtifactStoragePolicy(max_reserved_bytes_per_tenant=0)
    with pytest.raises(ValueError):
        ArtifactStoragePolicy(max_reserved_artifacts_per_tenant=0)
    with pytest.raises(ValueError):
        admit_artifact_storage(
            ArtifactStoragePolicy(),
            reserved_bytes=0,
            reserved_artifacts=0,
            requested_bytes=0,
        )


def test_tenant_storage_policy_keeps_workspace_and_artifact_pools_separate() -> None:
    deployment = TenantStoragePolicy(
        max_reserved_workspace_bytes=1_000,
        max_reserved_workspaces=10,
        max_reserved_artifact_bytes=2_000,
        max_reserved_artifacts=20,
    )
    tenant = TenantStoragePolicy(
        max_reserved_workspace_bytes=800,
        max_reserved_artifacts=12,
    )

    effective = deployment.narrowed_by(tenant)

    assert effective == TenantStoragePolicy(
        max_reserved_workspace_bytes=800,
        max_reserved_workspaces=10,
        max_reserved_artifact_bytes=2_000,
        max_reserved_artifacts=12,
    )
    assert effective.artifact_policy() == ArtifactStoragePolicy(
        max_reserved_bytes_per_tenant=2_000,
        max_reserved_artifacts_per_tenant=12,
    )


def test_tenant_storage_policy_reports_deployment_expansion() -> None:
    deployment = TenantStoragePolicy(
        max_reserved_workspace_bytes=1_000,
        max_reserved_artifacts=10,
    )
    tenant = TenantStoragePolicy(
        max_reserved_workspace_bytes=1_001,
        max_reserved_artifacts=11,
    )

    assert deployment.expansion_fields(tenant) == (
        "max_reserved_workspace_bytes",
        "max_reserved_artifacts",
    )


def test_workspace_storage_admission_uses_reserved_quota_not_live_usage() -> None:
    policy = TenantStoragePolicy(
        max_reserved_workspace_bytes=1_000,
        max_reserved_workspaces=2,
    )

    admit_workspace_storage(
        policy,
        reserved_bytes=700,
        reserved_workspaces=1,
        requested_bytes=300,
    )
    with pytest.raises(WorkspaceStorageAdmissionDenied) as denied:
        admit_workspace_storage(
            policy,
            reserved_bytes=700,
            reserved_workspaces=1,
            requested_bytes=301,
        )
    assert denied.value.reason_code == "WORKSPACE_TENANT_STORAGE_BYTES_LIMIT"

import shutil
from pathlib import Path

import yaml

from packages.security_acceptance.config import load_plan
from packages.security_acceptance.evaluator import evaluate_plan

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
SECURITY_ROOT = REPOSITORY_ROOT / "infra/security/ap-e7-006"


def test_dry_run_validates_supply_chain_and_sandbox_contracts() -> None:
    plan, base_directory = load_plan(SECURITY_ROOT / "plan.yaml")

    report = evaluate_plan(plan, base_directory=base_directory)

    assert report["status"] == "dry_run"
    assert report["blockers"] == []
    assert all(check["status"] == "PASS" for check in report["checks"])


def test_production_mode_rejects_placeholder_evidence() -> None:
    plan, base_directory = load_plan(SECURITY_ROOT / "plan.yaml")
    production_plan = plan.model_copy(update={"mode": "production"})

    report = evaluate_plan(production_plan, base_directory=base_directory)

    assert report["status"] == "failed"
    assert any("placeholder" in blocker for blocker in report["blockers"])


def test_production_mode_rejects_risk_exceptions(tmp_path: Path) -> None:
    for name in (
        "sandbox-workload.example.yaml",
        "sandbox-network-policy.example.yaml",
    ):
        shutil.copy(SECURITY_ROOT / name, tmp_path / name)
    evidence = yaml.safe_load(
        (SECURITY_ROOT / "runtime-agentscope-evidence.example.yaml").read_text(
            encoding="utf-8"
        )
    )
    evidence["risk_exceptions"] = [
        {
            "finding": "high_vulnerability",
            "accepted_by": "security-reviewer",
            "expires_at": "2026-09-01T00:00:00Z",
            "reason": "temporary mitigation while patch is prepared",
            "compensating_controls": ["sandbox isolation"],
        }
    ]
    (tmp_path / "evidence.yaml").write_text(
        yaml.safe_dump(evidence, sort_keys=False), encoding="utf-8"
    )
    plan, _ = load_plan(SECURITY_ROOT / "plan.yaml")
    production_plan = plan.model_copy(
        update={
            "mode": "production",
            "supply_chain_evidence": ["evidence.yaml"],
        }
    )

    report = evaluate_plan(production_plan, base_directory=tmp_path)

    assert any(
        check["name"] == "supply_chain.runtime-agentscope.production_risk_exceptions"
        and check["status"] == "FAIL"
        for check in report["checks"]
    )


def test_unsafe_sandbox_host_namespace_is_blocked(tmp_path: Path) -> None:
    for name in (
        "runtime-agentscope-evidence.example.yaml",
        "sandbox-network-policy.example.yaml",
    ):
        shutil.copy(SECURITY_ROOT / name, tmp_path / name)
    documents = list(
        yaml.safe_load_all(
            (SECURITY_ROOT / "sandbox-workload.example.yaml").read_text(
                encoding="utf-8"
            )
        )
    )
    pod = next(item for item in documents if item["kind"] == "Pod")
    pod["spec"]["hostPID"] = True
    (tmp_path / "sandbox.yaml").write_text(
        "---\n".join(yaml.safe_dump(item, sort_keys=False) for item in documents),
        encoding="utf-8",
    )
    plan, _ = load_plan(SECURITY_ROOT / "plan.yaml")
    local_plan = plan.model_copy(update={"sandbox_manifest": "sandbox.yaml"})

    report = evaluate_plan(local_plan, base_directory=tmp_path)

    assert any(
        check["name"] == "sandbox.host_namespaces" and check["status"] == "FAIL"
        for check in report["checks"]
    )
    assert report["blockers"]


def test_sandbox_image_digest_must_match_supply_chain_evidence(
    tmp_path: Path,
) -> None:
    for name in (
        "runtime-agentscope-evidence.example.yaml",
        "sandbox-network-policy.example.yaml",
    ):
        shutil.copy(SECURITY_ROOT / name, tmp_path / name)
    documents = list(
        yaml.safe_load_all(
            (SECURITY_ROOT / "sandbox-workload.example.yaml").read_text(
                encoding="utf-8"
            )
        )
    )
    pod = next(item for item in documents if item["kind"] == "Pod")
    pod["spec"]["containers"][0]["image"] = (
        "registry.example.invalid/runtime@sha256:" + "9" * 64
    )
    (tmp_path / "sandbox.yaml").write_text(
        "---\n".join(yaml.safe_dump(item, sort_keys=False) for item in documents),
        encoding="utf-8",
    )
    plan, _ = load_plan(SECURITY_ROOT / "plan.yaml")
    local_plan = plan.model_copy(update={"sandbox_manifest": "sandbox.yaml"})

    report = evaluate_plan(local_plan, base_directory=tmp_path)

    assert any(
        check["name"] == "sandbox.container.0.supply_chain_binding"
        and check["status"] == "FAIL"
        for check in report["checks"]
    )


def test_proxy_egress_rejects_wildcard_selectors(tmp_path: Path) -> None:
    for name in (
        "runtime-agentscope-evidence.example.yaml",
        "sandbox-workload.example.yaml",
    ):
        shutil.copy(SECURITY_ROOT / name, tmp_path / name)
    policy: dict[str, object] = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "unsafe"},
        "spec": {
            "podSelector": {"matchLabels": {"agent-platform.io/sandbox": "true"}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
            "egress": [
                {
                    "to": [
                        {
                            "namespaceSelector": {},
                            "podSelector": {},
                        }
                    ],
                    "ports": [{"protocol": "TCP", "port": 8443}],
                }
            ],
        },
    }
    (tmp_path / "network.yaml").write_text(
        yaml.safe_dump(policy, sort_keys=False), encoding="utf-8"
    )
    plan, _ = load_plan(SECURITY_ROOT / "plan.yaml")
    local_plan = plan.model_copy(
        update={
            "expected_network_mode": "proxy_allowlist",
            "network_policy_manifest": "network.yaml",
        }
    )

    report = evaluate_plan(local_plan, base_directory=tmp_path)

    assert any(
        check["name"] == "sandbox.network_policy_egress" and check["status"] == "FAIL"
        for check in report["checks"]
    )

from pathlib import Path

import pytest

from packages.security_acceptance.config import load_plan
from packages.security_acceptance.evaluator import evaluate_plan

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
PLAN_PATH = REPOSITORY_ROOT / "infra/security/ap-e7-006/plan.yaml"


def test_example_plan_loads_with_dry_run_mode() -> None:
    plan, base_directory = load_plan(PLAN_PATH)

    assert plan.mode == "dry_run"
    assert plan.expected_runtime_class == "gvisor"
    assert base_directory.name == "ap-e7-006"


def test_plan_rejects_path_outside_plan_directory(tmp_path: Path) -> None:
    path = tmp_path / "plan.yaml"
    path.write_text(
        """
plan_id: invalid
mode: dry_run
sandbox_manifest: ../sandbox.yaml
network_policy_manifest: network.yaml
supply_chain_evidence: [evidence.yaml]
""",
        encoding="utf-8",
    )

    plan, base_directory = load_plan(path)

    with pytest.raises(ValueError, match="stay under"):
        evaluate_plan(plan, base_directory=base_directory)


def test_evidence_uri_rejects_embedded_credentials(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.yaml"
    evidence.write_text(
        """
subject_name: image
subject_type: image
subject_digest: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
source:
  uri: https://user:password@example.invalid/source
  digest: sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
builder: ci
compiler_version: 1
built_at: 2026-08-14T00:00:00Z
sbom:
  uri: https://example.invalid/sbom
  digest: sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
vulnerability_scan:
  scanner: scanner
  scanner_version: 1
  status: PASSED
  report:
    uri: https://example.invalid/report
    digest: sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
  critical_count: 0
  high_count: 0
  known_exploitable_count: 0
license_scan:
  scanner: scanner
  scanner_version: 1
  status: PASSED
  report:
    uri: https://example.invalid/report
    digest: sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee
secret_scan:
  scanner: scanner
  scanner_version: 1
  status: PASSED
  report:
    uri: https://example.invalid/report
    digest: sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
malicious_code_scan:
  scanner: scanner
  scanner_version: 1
  status: PASSED
  report:
    uri: https://example.invalid/report
    digest: sha256:1111111111111111111111111111111111111111111111111111111111111111
signature:
  status: VERIFIED
  verifier: verifier
  identity: identity
  evidence:
    uri: https://example.invalid/signature
    digest: sha256:2222222222222222222222222222222222222222222222222222222222222222
provenance:
  status: VERIFIED
  verifier: verifier
  identity: identity
  evidence:
    uri: https://example.invalid/provenance
    digest: sha256:3333333333333333333333333333333333333333333333333333333333333333
reviewed_by: reviewer
reviewed_at: 2026-08-14T00:10:00Z
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="credentials"):
        from packages.security_acceptance.config import load_supply_chain_evidence

        load_supply_chain_evidence(evidence)

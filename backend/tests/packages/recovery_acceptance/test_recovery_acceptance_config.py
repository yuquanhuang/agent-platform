from pathlib import Path

import pytest
import yaml

from packages.recovery_acceptance.config import EvidenceReference, load_plan
from packages.recovery_acceptance.evaluator import (
    REQUIRED_READINESS_ITEMS,
    REQUIRED_SCENARIOS,
    evaluate_plan,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
RECOVERY_ROOT = REPOSITORY_ROOT / "infra/recovery/ap-e7-007"


def test_recovery_template_loads_with_expected_scope() -> None:
    plan, base_directory = load_plan(RECOVERY_ROOT / "plan.yaml")

    assert plan.mode == "dry_run"
    assert plan.max_drill_age_days == 92
    assert base_directory.name == "ap-e7-007"
    assert REQUIRED_SCENARIOS == {
        "postgresql_restore_and_integrity",
        "redis_loss_sse_database_recovery",
        "worker_temporal_rolling_restart",
        "sandbox_node_failure_credential_revoke",
        "object_storage_outage_artifact_retry",
        "model_gateway_limit_provider_failure",
        "outbox_dead_letter_replay_reconciliation",
    }
    assert "security_acceptance" in REQUIRED_READINESS_ITEMS


def test_plan_rejects_path_outside_plan_directory(tmp_path: Path) -> None:
    path = tmp_path / "plan.yaml"
    path.write_text(
        """
plan_id: invalid
mode: dry_run
as_of: 2026-08-14T00:00:00Z
evidence_file: ../evidence.yaml
""",
        encoding="utf-8",
    )

    plan, base_directory = load_plan(path)

    with pytest.raises(ValueError, match="stay under"):
        evaluate_plan(plan, base_directory=base_directory)


def test_evidence_uri_rejects_credentials(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.yaml"
    evidence.write_text(
        """
uri: https://user:password@example.invalid/evidence
digest: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="credentials"):
        EvidenceReference.model_validate(yaml.safe_load(evidence.read_text()))

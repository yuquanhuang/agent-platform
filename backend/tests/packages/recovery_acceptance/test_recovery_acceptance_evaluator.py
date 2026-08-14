from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

import yaml

from packages.recovery_acceptance.config import RecoveryAcceptancePlan, load_plan
from packages.recovery_acceptance.evaluator import evaluate_plan

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
RECOVERY_ROOT = REPOSITORY_ROOT / "infra/recovery/ap-e7-007"


def _reference(index: int) -> dict[str, str]:
    return {
        "uri": f"artifact://recovery/ap-e7-007-{index}.json",
        "digest": "sha256:" + str(index % 10) * 64,
    }


def _valid_evidence() -> dict[str, object]:
    timestamp = "2026-08-14T00:00:00Z"
    services: list[dict[str, object]] = []
    for index, service in enumerate(
        ("postgresql", "object_storage", "redis", "temporal"), start=1
    ):
        services.append(
            {
                "service": service,
                "status": "PASS",
                "started_at": timestamp,
                "ended_at": "2026-08-14T00:10:00Z",
                "observed_rpo_seconds": 120 if service == "postgresql" else 600,
                "observed_rto_seconds": 600,
                "data_loss_scope": (
                    "notification_only" if service == "redis" else "none"
                ),
                "evidence": [_reference(index)],
                "integrity_checks": [
                    {"name": "fact-hash", "status": "PASS", "details": "match"}
                ],
            }
        )
    scenario_ids = (
        "postgresql_restore_and_integrity",
        "redis_loss_sse_database_recovery",
        "worker_temporal_rolling_restart",
        "sandbox_node_failure_credential_revoke",
        "object_storage_outage_artifact_retry",
        "model_gateway_limit_provider_failure",
        "outbox_dead_letter_replay_reconciliation",
    )
    scenarios = [
        {
            "scenario_id": scenario_id,
            "status": "PASS",
            "started_at": timestamp,
            "ended_at": "2026-08-14T00:10:00Z",
            "evidence": [_reference(index + 10)],
            "notes": "verified",
        }
        for index, scenario_id in enumerate(scenario_ids)
    ]
    readiness_ids = (
        "contracts_and_generated_clients",
        "database_migrations_and_restore",
        "runtime_event_workflow_replay",
        "security_acceptance",
        "capacity_acceptance",
        "supply_chain_evidence",
        "observability_alerting_runbooks",
        "data_retention_backup_policy",
        "production_runtime_composition",
        "known_risks_closed",
    )
    readiness = [
        {
            "item_id": item_id,
            "status": "PASS",
            "evidence": [_reference(index + 20)],
            "notes": "verified",
        }
        for index, item_id in enumerate(readiness_ids)
    ]
    return {
        "drill_id": "drill-1",
        "environment": "isolated-kubernetes-lab",
        "started_at": timestamp,
        "ended_at": "2026-08-14T00:10:00Z",
        "conducted_by": ["operator"],
        "approved_by": "approver",
        "services": services,
        "scenarios": scenarios,
        "readiness": readiness,
    }


def _plan(
    *, mode: Literal["dry_run", "production"] = "production"
) -> RecoveryAcceptancePlan:
    return RecoveryAcceptancePlan(
        plan_id="plan-1",
        mode=mode,
        as_of=datetime(2026, 8, 14, 1, tzinfo=UTC),
        evidence_file="evidence.yaml",
    )


def test_production_evidence_passes_when_all_objectives_and_readiness_pass(
    tmp_path: Path,
) -> None:
    (tmp_path / "evidence.yaml").write_text(
        yaml.safe_dump(_valid_evidence(), sort_keys=False), encoding="utf-8"
    )

    report = evaluate_plan(
        _plan(),
        base_directory=tmp_path,
        now=datetime(2026, 8, 14, 1, tzinfo=UTC),
    )

    assert report["status"] == "passed"
    assert report["blockers"] == []


def test_production_rejects_placeholder_evidence(tmp_path: Path) -> None:
    evidence = _valid_evidence()
    evidence["environment"] = "recovery.example.invalid"
    (tmp_path / "evidence.yaml").write_text(
        yaml.safe_dump(evidence, sort_keys=False), encoding="utf-8"
    )

    report = evaluate_plan(
        _plan(),
        base_directory=tmp_path,
        now=datetime(2026, 8, 14, 1, tzinfo=UTC),
    )

    assert report["status"] == "failed"
    assert any("placeholders" in blocker for blocker in report["blockers"])


def test_production_rejects_rto_above_objective(tmp_path: Path) -> None:
    evidence = _valid_evidence()
    services = cast(list[dict[str, object]], evidence["services"])
    postgresql = services[0]
    assert isinstance(postgresql, dict)
    postgresql["ended_at"] = "2026-08-14T01:10:00Z"
    postgresql["observed_rto_seconds"] = 4200
    evidence["ended_at"] = "2026-08-14T01:10:00Z"
    (tmp_path / "evidence.yaml").write_text(
        yaml.safe_dump(evidence, sort_keys=False), encoding="utf-8"
    )

    report = evaluate_plan(
        _plan(),
        base_directory=tmp_path,
        now=datetime(2026, 8, 14, 1, 10, tzinfo=UTC),
    )

    assert report["status"] == "failed"
    assert any("RTO" in blocker for blocker in report["blockers"])


def test_production_rejects_stale_evaluation_time(tmp_path: Path) -> None:
    (tmp_path / "evidence.yaml").write_text(
        yaml.safe_dump(_valid_evidence(), sort_keys=False), encoding="utf-8"
    )

    report = evaluate_plan(
        _plan(),
        base_directory=tmp_path,
        now=datetime(2026, 8, 14, 2, tzinfo=UTC),
    )

    assert report["status"] == "failed"
    assert any("five minutes" in blocker for blocker in report["blockers"])


def test_dry_run_template_reports_unexecuted_work_without_production_failure() -> None:
    plan, base_directory = load_plan(RECOVERY_ROOT / "plan.yaml")
    report = evaluate_plan(
        plan,
        base_directory=base_directory,
        now=datetime(2026, 8, 14, 1, tzinfo=UTC),
    )

    assert report["status"] == "dry_run"
    assert report["blockers"]

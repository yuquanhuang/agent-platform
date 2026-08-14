"""Fail-closed RPO/RTO and production-readiness evaluation for AP-E7-007."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.recovery_acceptance.config import (
    RecoveryAcceptanceEvidence,
    RecoveryAcceptancePlan,
    load_evidence,
)
from packages.recovery_acceptance.report import RecoveryAcceptanceReport

SERVICE_OBJECTIVES: dict[str, tuple[int | None, int, str | None]] = {
    "postgresql": (300, 3600, None),
    "object_storage": (900, 14400, None),
    "redis": (None, 1800, "notification_only"),
    "temporal": (None, 3600, "none"),
}
REQUIRED_SCENARIOS = frozenset(
    {
        "postgresql_restore_and_integrity",
        "redis_loss_sse_database_recovery",
        "worker_temporal_rolling_restart",
        "sandbox_node_failure_credential_revoke",
        "object_storage_outage_artifact_retry",
        "model_gateway_limit_provider_failure",
        "outbox_dead_letter_replay_reconciliation",
    }
)
REQUIRED_READINESS_ITEMS = frozenset(
    {
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
    }
)


def evaluate_plan(
    plan: RecoveryAcceptancePlan,
    *,
    base_directory: Path,
    now: datetime | None = None,
) -> RecoveryAcceptanceReport:
    evidence_path = _resolve_plan_path(base_directory, plan.evidence_file)
    evidence = load_evidence(evidence_path)
    instant = now or datetime.now(UTC)
    report = RecoveryAcceptanceReport.new(
        plan_id=plan.plan_id, mode=plan.mode, metadata=plan.metadata
    )
    _evaluate_drill_window(report, plan, evidence, instant)
    _evaluate_services(report, evidence)
    _evaluate_scenarios(report, evidence)
    _evaluate_readiness(report, evidence)
    report["follow_up"] = [
        "Attach immutable backup, restore, integrity and metric evidence from the isolated environment.",
        "Close every recovery finding before production admission; do not waive missing drills with dry-run output.",
        "Repeat the recovery drill at least quarterly and record observed RPO/RTO and corrective-action closure.",
    ]
    report.finish()
    return report


def _evaluate_drill_window(
    report: RecoveryAcceptanceReport,
    plan: RecoveryAcceptancePlan,
    evidence: RecoveryAcceptanceEvidence,
    instant: datetime,
) -> None:
    age_days = (instant - evidence.ended_at).total_seconds() / 86400
    if plan.mode == "production":
        plan_age_seconds = abs((instant - plan.as_of).total_seconds())
        report.add_check(
            name="drill.evaluation_time",
            passed=plan_age_seconds <= 300,
            message="production plan as_of must be within five minutes of evaluation",
        )
    report.add_check(
        name="drill.environment",
        passed=not _contains_placeholder(evidence.environment),
        message="drill environment must identify a real isolated target",
    )
    report.add_check(
        name="drill.approval",
        passed=not _contains_placeholder(evidence.approved_by),
        message="drill must have a named approver",
    )
    report.add_check(
        name="drill.freshness",
        passed=0 <= age_days <= plan.max_drill_age_days,
        message=f"drill evidence must be no older than {plan.max_drill_age_days} days",
    )
    if plan.mode == "production":
        serialized = evidence.model_dump_json()
        report.add_check(
            name="drill.production_evidence",
            passed=not _contains_placeholder(serialized),
            message="production recovery evidence must not contain placeholders",
        )


def _evaluate_services(
    report: RecoveryAcceptanceReport, evidence: RecoveryAcceptanceEvidence
) -> None:
    observed = {item.service: item for item in evidence.services}
    for service, (max_rpo, max_rto, loss_scope) in SERVICE_OBJECTIVES.items():
        item = observed.get(service)
        prefix = f"service.{service}"
        report.add_check(
            name=f"{prefix}.present",
            passed=item is not None,
            message=f"{service} recovery evidence is required",
        )
        if item is None:
            continue
        report.add_check(
            name=f"{prefix}.status",
            passed=item.status == "PASS",
            message=f"{service} recovery status must be PASS",
        )
        report.add_check(
            name=f"{prefix}.rto",
            passed=item.observed_rto_seconds is not None
            and item.observed_rto_seconds <= max_rto,
            message=f"{service} observed RTO must be <= {max_rto} seconds",
        )
        rpo_passed = max_rpo is None or (
            item.observed_rpo_seconds is not None
            and item.observed_rpo_seconds <= max_rpo
        )
        report.add_check(
            name=f"{prefix}.rpo",
            passed=rpo_passed,
            message=(
                f"{service} observed RPO must be <= {max_rpo} seconds"
                if max_rpo is not None
                else f"{service} RPO is not a business-data objective"
            ),
        )
        if loss_scope is not None:
            report.add_check(
                name=f"{prefix}.data_loss_scope",
                passed=item.data_loss_scope == loss_scope,
                message=f"{service} data loss scope must be {loss_scope}",
            )
        report.add_check(
            name=f"{prefix}.integrity",
            passed=bool(item.integrity_checks)
            and all(check.status == "PASS" for check in item.integrity_checks),
            message=f"{service} integrity checks must all pass",
        )
        report.add_check(
            name=f"{prefix}.evidence",
            passed=bool(item.evidence),
            message=f"{service} must attach immutable recovery evidence",
        )


def _evaluate_scenarios(
    report: RecoveryAcceptanceReport, evidence: RecoveryAcceptanceEvidence
) -> None:
    observed = {item.scenario_id: item for item in evidence.scenarios}
    for scenario_id in sorted(REQUIRED_SCENARIOS):
        item = observed.get(scenario_id)
        report.add_check(
            name=f"scenario.{scenario_id}.present",
            passed=item is not None,
            message=f"required recovery scenario {scenario_id} must be present",
        )
        if item is None:
            continue
        report.add_check(
            name=f"scenario.{scenario_id}.status",
            passed=item.status == "PASS",
            message=f"recovery scenario {scenario_id} must be PASS",
        )
        report.add_check(
            name=f"scenario.{scenario_id}.evidence",
            passed=bool(item.evidence),
            message=f"recovery scenario {scenario_id} must attach evidence",
        )


def _evaluate_readiness(
    report: RecoveryAcceptanceReport, evidence: RecoveryAcceptanceEvidence
) -> None:
    observed = {item.item_id: item for item in evidence.readiness}
    for item_id in sorted(REQUIRED_READINESS_ITEMS):
        item = observed.get(item_id)
        report.add_check(
            name=f"readiness.{item_id}.present",
            passed=item is not None,
            message=f"production readiness item {item_id} must be present",
        )
        if item is None:
            continue
        report.add_check(
            name=f"readiness.{item_id}.status",
            passed=item.status == "PASS",
            message=f"production readiness item {item_id} must be PASS",
        )
        report.add_check(
            name=f"readiness.{item_id}.evidence",
            passed=bool(item.evidence),
            message=f"production readiness item {item_id} must attach evidence",
        )


def _resolve_plan_path(base_directory: Path, relative_path: str) -> Path:
    candidate = (base_directory / relative_path).resolve()
    base = base_directory.resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError("recovery acceptance paths must stay under the plan directory")
    return candidate


def _contains_placeholder(value: Any) -> bool:
    text = str(value).lower()
    return any(
        marker in text
        for marker in ("example.invalid", "replace-with", "pending-", "tbd", "todo")
    )

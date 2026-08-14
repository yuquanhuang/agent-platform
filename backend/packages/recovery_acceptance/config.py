"""Validated AP-E7-007 recovery drill evidence and plan models."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SHA256_PATTERN = r"^sha256:[a-f0-9]{64}$"


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    uri: str = Field(min_length=1, max_length=2048)
    digest: str = Field(pattern=SHA256_PATTERN)

    @field_validator("uri")
    @classmethod
    def validate_uri(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "s3", "artifact"}:
            raise ValueError("evidence URI must use https, s3 or artifact")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "evidence URI must not contain credentials, query or fragment"
            )
        return value


class IntegrityCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=200)
    status: Literal["PASS", "FAIL"]
    details: str = Field(min_length=1, max_length=2000)


class RecoveryServiceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service: Literal["postgresql", "object_storage", "redis", "temporal"]
    status: Literal["PASS", "FAIL", "NOT_RUN"]
    started_at: datetime
    ended_at: datetime
    observed_rpo_seconds: int | None = Field(default=None, ge=0)
    observed_rto_seconds: int | None = Field(default=None, ge=0)
    data_loss_scope: Literal["none", "notification_only", "business"]
    evidence: list[EvidenceReference] = Field(
        default_factory=list[EvidenceReference], max_length=50
    )
    integrity_checks: list[IntegrityCheck] = Field(
        default_factory=list[IntegrityCheck], max_length=50
    )

    @model_validator(mode="after")
    def validate_window(self) -> RecoveryServiceEvidence:
        _require_utc(self.started_at, "started_at")
        _require_utc(self.ended_at, "ended_at")
        if self.ended_at < self.started_at:
            raise ValueError("ended_at cannot precede started_at")
        if self.service == "redis" and self.data_loss_scope != "notification_only":
            raise ValueError("Redis recovery must declare notification_only data loss")
        if self.status == "PASS":
            if self.observed_rto_seconds is None:
                raise ValueError("PASS service recovery requires observed_rto_seconds")
            elapsed_seconds = int((self.ended_at - self.started_at).total_seconds())
            if self.observed_rto_seconds != elapsed_seconds:
                raise ValueError(
                    "observed_rto_seconds must match the recorded recovery window"
                )
            if not self.evidence or not self.integrity_checks:
                raise ValueError(
                    "PASS service recovery requires evidence and integrity checks"
                )
            if any(check.status != "PASS" for check in self.integrity_checks):
                raise ValueError("PASS service recovery cannot contain failed checks")
        return self


class RecoveryScenarioEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str = Field(min_length=1, max_length=128)
    status: Literal["PASS", "FAIL", "NOT_RUN"]
    started_at: datetime
    ended_at: datetime
    evidence: list[EvidenceReference] = Field(
        default_factory=list[EvidenceReference], max_length=50
    )
    notes: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def validate_window(self) -> RecoveryScenarioEvidence:
        _require_utc(self.started_at, "started_at")
        _require_utc(self.ended_at, "ended_at")
        if self.ended_at < self.started_at:
            raise ValueError("ended_at cannot precede started_at")
        if self.status == "PASS" and not self.evidence:
            raise ValueError("PASS recovery scenario requires evidence")
        return self


class ReadinessItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: str = Field(min_length=1, max_length=128)
    status: Literal["PASS", "BLOCKED", "NOT_RUN"]
    evidence: list[EvidenceReference] = Field(
        default_factory=list[EvidenceReference], max_length=50
    )
    notes: str = Field(default="", max_length=4000)


class RecoveryAcceptanceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    drill_id: str = Field(min_length=1, max_length=128)
    environment: str = Field(min_length=1, max_length=200)
    started_at: datetime
    ended_at: datetime
    conducted_by: list[str] = Field(min_length=1, max_length=20)
    approved_by: str = Field(min_length=1, max_length=200)
    services: list[RecoveryServiceEvidence] = Field(min_length=1, max_length=20)
    scenarios: list[RecoveryScenarioEvidence] = Field(min_length=1, max_length=30)
    readiness: list[ReadinessItem] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_evidence(self) -> RecoveryAcceptanceEvidence:
        _require_utc(self.started_at, "started_at")
        _require_utc(self.ended_at, "ended_at")
        if self.ended_at < self.started_at:
            raise ValueError("ended_at cannot precede started_at")
        for item in [*self.services, *self.scenarios]:
            if item.started_at < self.started_at or item.ended_at > self.ended_at:
                raise ValueError(
                    "service and scenario windows must stay within the drill"
                )
        service_ids = [item.service for item in self.services]
        if len(service_ids) != len(set(service_ids)):
            raise ValueError("recovery services must be unique")
        scenario_ids = [item.scenario_id for item in self.scenarios]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("recovery scenarios must be unique")
        readiness_ids = [item.item_id for item in self.readiness]
        if len(readiness_ids) != len(set(readiness_ids)):
            raise ValueError("readiness items must be unique")
        return self


class RecoveryAcceptancePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str = Field(min_length=1, max_length=128)
    mode: Literal["dry_run", "production"] = "dry_run"
    as_of: datetime
    max_drill_age_days: int = Field(default=92, ge=1, le=92)
    evidence_file: str = Field(min_length=1, max_length=1024)
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("as_of")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        return _require_utc(value, "as_of")


def _require_utc(value: datetime, name: str) -> datetime:
    offset = value.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value


def load_plan(path: str | Path) -> tuple[RecoveryAcceptancePlan, Path]:
    plan_path = Path(path).resolve()
    payload = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("recovery acceptance plan must contain a mapping")
    return RecoveryAcceptancePlan.model_validate(payload), plan_path.parent


def load_evidence(path: Path) -> RecoveryAcceptanceEvidence:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("recovery evidence must contain a mapping")
    return RecoveryAcceptanceEvidence.model_validate(payload)

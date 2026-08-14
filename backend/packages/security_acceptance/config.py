"""Validated AP-E7-006 plan and supply-chain evidence models."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SHA256_PATTERN = r"^sha256:[a-f0-9]{64}$"


class EvidenceDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    uri: str = Field(min_length=1, max_length=2048)
    digest: str = Field(pattern=SHA256_PATTERN)

    @field_validator("uri")
    @classmethod
    def validate_uri(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "oci", "s3", "artifact"}:
            raise ValueError("evidence URI must use https, oci, s3 or artifact")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "evidence URI must not contain credentials, query or fragment"
            )
        return value


class ScanEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scanner: str = Field(min_length=1, max_length=200)
    scanner_version: str = Field(min_length=1, max_length=100)
    status: Literal["PASSED", "FAILED"]
    report: EvidenceDocument


class VulnerabilityEvidence(ScanEvidence):
    critical_count: int = Field(ge=0)
    high_count: int = Field(ge=0)
    known_exploitable_count: int = Field(ge=0)


class VerificationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["VERIFIED", "UNVERIFIED", "NOT_PROVIDED"]
    verifier: str = Field(min_length=1, max_length=200)
    identity: str = Field(min_length=1, max_length=500)
    evidence: EvidenceDocument


class RiskException(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    finding: Literal[
        "critical_vulnerability",
        "high_vulnerability",
        "known_exploitable_vulnerability",
    ]
    accepted_by: str = Field(min_length=1, max_length=200)
    expires_at: datetime
    reason: str = Field(min_length=8, max_length=2000)
    compensating_controls: list[str] = Field(min_length=1, max_length=20)


class SupplyChainEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject_name: str = Field(min_length=1, max_length=200)
    subject_type: Literal["image", "bundle", "skill", "mcp", "dependency"]
    subject_digest: str = Field(pattern=SHA256_PATTERN)
    source: EvidenceDocument
    builder: str = Field(min_length=1, max_length=200)
    compiler_version: str = Field(min_length=1, max_length=100)
    built_at: datetime
    sbom: EvidenceDocument
    vulnerability_scan: VulnerabilityEvidence
    license_scan: ScanEvidence
    secret_scan: ScanEvidence
    malicious_code_scan: ScanEvidence
    signature: VerificationEvidence
    provenance: VerificationEvidence
    reviewed_by: str = Field(min_length=1, max_length=200)
    reviewed_at: datetime
    risk_exceptions: list[RiskException] = Field(
        default_factory=list[RiskException], max_length=20
    )

    @model_validator(mode="after")
    def validate_timestamps(self) -> SupplyChainEvidence:
        for name in ("built_at", "reviewed_at"):
            value = getattr(self, name)
            if value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
                raise ValueError(f"{name} must be timezone-aware UTC")
        if self.reviewed_at < self.built_at:
            raise ValueError("reviewed_at cannot precede built_at")
        return self


class SecurityAcceptancePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str = Field(min_length=1, max_length=128)
    mode: Literal["dry_run", "production"] = "dry_run"
    expected_runtime_class: str = Field(default="gvisor", min_length=1, max_length=63)
    expected_network_mode: Literal["none", "proxy_allowlist"] = "none"
    sandbox_manifest: str = Field(min_length=1, max_length=1024)
    network_policy_manifest: str = Field(min_length=1, max_length=1024)
    supply_chain_evidence: list[str] = Field(min_length=1, max_length=100)
    metadata: dict[str, str] = Field(default_factory=dict)


def load_plan(path: str | Path) -> tuple[SecurityAcceptancePlan, Path]:
    plan_path = Path(path).resolve()
    payload = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("security acceptance plan must contain a mapping")
    return SecurityAcceptancePlan.model_validate(payload), plan_path.parent


def load_supply_chain_evidence(path: Path) -> SupplyChainEvidence:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("supply-chain evidence must contain a mapping")
    return SupplyChainEvidence.model_validate(payload)

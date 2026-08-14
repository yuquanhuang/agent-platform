"""Fail-closed Admission Controller for immutable Runtime Bundles."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from packages.domain.bundles import BUNDLE_COMPILER_NAME, BUNDLE_COMPILER_VERSION

_HASH_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


class AdmissionDenied(ValueError):
    """A stable internal denial that callers map to their frozen API errors."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class CapacityAdmissionDenied(ValueError):
    """A stable Run capacity denial mapped to the frozen 429 error."""

    def __init__(
        self,
        *,
        scope: Literal["tenant", "user", "agent", "runtime"],
        reason_code: str,
        current: int,
        limit: int,
    ) -> None:
        self.scope = scope
        self.reason_code = reason_code
        self.current = current
        self.limit = limit
        super().__init__("Run capacity is temporarily exhausted.")


class ArtifactStorageAdmissionDenied(ValueError):
    """A stable Artifact storage denial mapped to the frozen 429 error."""

    def __init__(
        self,
        *,
        dimension: Literal["bytes", "artifacts"],
        reason_code: str,
        current: int,
        requested: int,
        limit: int,
    ) -> None:
        self.dimension = dimension
        self.reason_code = reason_code
        self.current = current
        self.requested = requested
        self.limit = limit
        super().__init__("Artifact storage capacity is temporarily exhausted.")


class WorkspaceStorageAdmissionDenied(ValueError):
    """A stable aggregate Workspace storage denial mapped to the frozen 429."""

    def __init__(
        self,
        *,
        dimension: Literal["bytes", "workspaces"],
        reason_code: str,
        current: int,
        requested: int,
        limit: int,
    ) -> None:
        self.dimension = dimension
        self.reason_code = reason_code
        self.current = current
        self.requested = requested
        self.limit = limit
        self.storage_policy_version_id: object | None = None
        super().__init__("Workspace storage capacity is temporarily exhausted.")


@dataclass(frozen=True, slots=True)
class ArtifactStoragePolicy:
    """Deployment-owned tenant hard limits for reserved Artifact storage."""

    max_reserved_bytes_per_tenant: int | None = None
    max_reserved_artifacts_per_tenant: int | None = None

    def __post_init__(self) -> None:
        if self.max_reserved_bytes_per_tenant is not None and not (
            1 <= self.max_reserved_bytes_per_tenant <= 1_125_899_906_842_624
        ):
            raise ValueError(
                "max_reserved_bytes_per_tenant must be between 1 and 1125899906842624"
            )
        if self.max_reserved_artifacts_per_tenant is not None and not (
            1 <= self.max_reserved_artifacts_per_tenant <= 1_000_000_000
        ):
            raise ValueError(
                "max_reserved_artifacts_per_tenant must be between 1 and 1000000000"
            )

    @property
    def enabled(self) -> bool:
        return (
            self.max_reserved_bytes_per_tenant is not None
            or self.max_reserved_artifacts_per_tenant is not None
        )


_STORAGE_POLICY_FIELDS = (
    "max_reserved_workspace_bytes",
    "max_reserved_workspaces",
    "max_reserved_artifact_bytes",
    "max_reserved_artifacts",
)


@dataclass(frozen=True, slots=True)
class TenantStoragePolicy:
    """Hard limits for separate tenant Workspace and Artifact storage pools."""

    max_reserved_workspace_bytes: int | None = None
    max_reserved_workspaces: int | None = None
    max_reserved_artifact_bytes: int | None = None
    max_reserved_artifacts: int | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "max_reserved_workspace_bytes",
            "max_reserved_artifact_bytes",
        ):
            value = getattr(self, field_name)
            if value is not None and not 1 <= value <= 1_125_899_906_842_624:
                raise ValueError(f"{field_name} must be between 1 and 1125899906842624")
        for field_name in ("max_reserved_workspaces", "max_reserved_artifacts"):
            value = getattr(self, field_name)
            if value is not None and not 1 <= value <= 1_000_000_000:
                raise ValueError(f"{field_name} must be between 1 and 1000000000")

    @property
    def enabled(self) -> bool:
        return any(
            getattr(self, field_name) is not None
            for field_name in _STORAGE_POLICY_FIELDS
        )

    def narrowed_by(self, tenant_policy: TenantStoragePolicy) -> TenantStoragePolicy:
        """Combine deployment and tenant limits without sharing resource pools."""

        def effective(platform: int | None, tenant: int | None) -> int | None:
            if platform is None:
                return tenant
            if tenant is None:
                return platform
            return min(platform, tenant)

        return TenantStoragePolicy(
            **{
                field_name: effective(
                    getattr(self, field_name), getattr(tenant_policy, field_name)
                )
                for field_name in _STORAGE_POLICY_FIELDS
            }
        )

    def expansion_fields(self, tenant_policy: TenantStoragePolicy) -> tuple[str, ...]:
        return tuple(
            field_name
            for field_name in _STORAGE_POLICY_FIELDS
            if (platform := getattr(self, field_name)) is not None
            and (tenant := getattr(tenant_policy, field_name)) is not None
            and tenant > platform
        )

    def artifact_policy(self) -> ArtifactStoragePolicy:
        return ArtifactStoragePolicy(
            max_reserved_bytes_per_tenant=self.max_reserved_artifact_bytes,
            max_reserved_artifacts_per_tenant=self.max_reserved_artifacts,
        )


def admit_artifact_storage(
    policy: ArtifactStoragePolicy,
    *,
    reserved_bytes: int,
    reserved_artifacts: int,
    requested_bytes: int,
) -> None:
    """Reject before reserving an upload that would exceed a tenant hard limit."""

    if reserved_bytes < 0 or reserved_artifacts < 0 or requested_bytes < 1:
        raise ValueError("Artifact storage admission facts are invalid")
    byte_limit = policy.max_reserved_bytes_per_tenant
    if byte_limit is not None and reserved_bytes + requested_bytes > byte_limit:
        raise ArtifactStorageAdmissionDenied(
            dimension="bytes",
            reason_code="ARTIFACT_TENANT_STORAGE_BYTES_LIMIT",
            current=reserved_bytes,
            requested=requested_bytes,
            limit=byte_limit,
        )
    artifact_limit = policy.max_reserved_artifacts_per_tenant
    if artifact_limit is not None and reserved_artifacts + 1 > artifact_limit:
        raise ArtifactStorageAdmissionDenied(
            dimension="artifacts",
            reason_code="ARTIFACT_TENANT_STORAGE_COUNT_LIMIT",
            current=reserved_artifacts,
            requested=1,
            limit=artifact_limit,
        )


def admit_workspace_storage(
    policy: TenantStoragePolicy,
    *,
    reserved_bytes: int,
    reserved_workspaces: int,
    requested_bytes: int,
) -> None:
    """Reject before reserving a Workspace that exceeds its separate pool."""

    if reserved_bytes < 0 or reserved_workspaces < 0 or requested_bytes < 1:
        raise ValueError("Workspace storage admission facts are invalid")
    byte_limit = policy.max_reserved_workspace_bytes
    if byte_limit is not None and reserved_bytes + requested_bytes > byte_limit:
        raise WorkspaceStorageAdmissionDenied(
            dimension="bytes",
            reason_code="WORKSPACE_TENANT_STORAGE_BYTES_LIMIT",
            current=reserved_bytes,
            requested=requested_bytes,
            limit=byte_limit,
        )
    workspace_limit = policy.max_reserved_workspaces
    if workspace_limit is not None and reserved_workspaces + 1 > workspace_limit:
        raise WorkspaceStorageAdmissionDenied(
            dimension="workspaces",
            reason_code="WORKSPACE_TENANT_STORAGE_COUNT_LIMIT",
            current=reserved_workspaces,
            requested=1,
            limit=workspace_limit,
        )


@dataclass(frozen=True, slots=True)
class RunCapacityPolicy:
    """Deployment-owned hard limits used before a Run fact is created."""

    max_nonterminal_runs_per_tenant: int | None = None
    max_nonterminal_runs_per_user: int | None = None
    max_nonterminal_runs_per_agent: int | None = None
    max_nonterminal_agentscope_runs: int | None = None
    max_nonterminal_codex_runs: int | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "max_nonterminal_runs_per_tenant",
            "max_nonterminal_runs_per_user",
            "max_nonterminal_runs_per_agent",
            "max_nonterminal_agentscope_runs",
            "max_nonterminal_codex_runs",
        ):
            value = getattr(self, field_name)
            if value is not None and not 1 <= value <= 1_000_000:
                raise ValueError(f"{field_name} must be between 1 and 1000000")

    @property
    def enabled(self) -> bool:
        return any(
            value is not None
            for value in (
                self.max_nonterminal_runs_per_tenant,
                self.max_nonterminal_runs_per_user,
                self.max_nonterminal_runs_per_agent,
                self.max_nonterminal_agentscope_runs,
                self.max_nonterminal_codex_runs,
            )
        )

    def runtime_limit(self, runtime_type: Literal["agentscope", "codex"]) -> int | None:
        return (
            self.max_nonterminal_agentscope_runs
            if runtime_type == "agentscope"
            else self.max_nonterminal_codex_runs
        )

    def narrowed_by(self, tenant_policy: RunCapacityPolicy) -> RunCapacityPolicy:
        """Combine deployment and tenant limits without allowing tenant expansion."""

        def effective(
            platform_limit: int | None, tenant_limit: int | None
        ) -> int | None:
            if platform_limit is None:
                return tenant_limit
            if tenant_limit is None:
                return platform_limit
            return min(platform_limit, tenant_limit)

        return RunCapacityPolicy(
            max_nonterminal_runs_per_tenant=effective(
                self.max_nonterminal_runs_per_tenant,
                tenant_policy.max_nonterminal_runs_per_tenant,
            ),
            max_nonterminal_runs_per_user=effective(
                self.max_nonterminal_runs_per_user,
                tenant_policy.max_nonterminal_runs_per_user,
            ),
            max_nonterminal_runs_per_agent=effective(
                self.max_nonterminal_runs_per_agent,
                tenant_policy.max_nonterminal_runs_per_agent,
            ),
            max_nonterminal_agentscope_runs=effective(
                self.max_nonterminal_agentscope_runs,
                tenant_policy.max_nonterminal_agentscope_runs,
            ),
            max_nonterminal_codex_runs=effective(
                self.max_nonterminal_codex_runs,
                tenant_policy.max_nonterminal_codex_runs,
            ),
        )

    def expansion_fields(self, tenant_policy: RunCapacityPolicy) -> tuple[str, ...]:
        """Return tenant dimensions that exceed an explicit deployment hard limit."""

        fields: list[str] = []
        for field_name in (
            "max_nonterminal_runs_per_tenant",
            "max_nonterminal_runs_per_user",
            "max_nonterminal_runs_per_agent",
            "max_nonterminal_agentscope_runs",
            "max_nonterminal_codex_runs",
        ):
            platform_limit = getattr(self, field_name)
            tenant_limit = getattr(tenant_policy, field_name)
            if (
                platform_limit is not None
                and tenant_limit is not None
                and tenant_limit > platform_limit
            ):
                fields.append(field_name)
        return tuple(fields)


@dataclass(frozen=True, slots=True)
class RunCapacityFacts:
    tenant_nonterminal_runs: int
    user_nonterminal_runs: int
    agent_nonterminal_runs: int
    runtime_nonterminal_runs: int
    runtime_type: Literal["agentscope", "codex"]


def admit_run_capacity(policy: RunCapacityPolicy, facts: RunCapacityFacts) -> None:
    """Reject before creating a Run when any configured hard limit is full."""

    checks: tuple[
        tuple[
            Literal["tenant", "user", "agent", "runtime"],
            str,
            int,
            int | None,
        ],
        ...,
    ] = (
        (
            "tenant",
            "RUN_TENANT_CONCURRENCY_LIMIT",
            facts.tenant_nonterminal_runs,
            policy.max_nonterminal_runs_per_tenant,
        ),
        (
            "user",
            "RUN_USER_CONCURRENCY_LIMIT",
            facts.user_nonterminal_runs,
            policy.max_nonterminal_runs_per_user,
        ),
        (
            "agent",
            "RUN_AGENT_CONCURRENCY_LIMIT",
            facts.agent_nonterminal_runs,
            policy.max_nonterminal_runs_per_agent,
        ),
        (
            "runtime",
            "RUN_RUNTIME_CONCURRENCY_LIMIT",
            facts.runtime_nonterminal_runs,
            policy.runtime_limit(facts.runtime_type),
        ),
    )
    for scope, reason_code, current, limit in checks:
        if limit is not None and current >= limit:
            raise CapacityAdmissionDenied(
                scope=scope,
                reason_code=reason_code,
                current=current,
                limit=limit,
            )


@dataclass(frozen=True, slots=True)
class RuntimeBundleAdmissionFacts:
    compiler_name: str
    compiler_version: str
    scan_status: str
    manifest: Mapping[str, object]


def admit_runtime_bundle(facts: RuntimeBundleAdmissionFacts) -> None:
    """Require a scanned Bundle compiled under the effective-policy baseline."""

    if facts.scan_status != "PASSED":
        raise AdmissionDenied(
            "BUNDLE_SCAN_NOT_PASSED",
            "The Runtime Bundle has not passed security scanning.",
        )
    if (
        facts.compiler_name != BUNDLE_COMPILER_NAME
        or facts.compiler_version != BUNDLE_COMPILER_VERSION
    ):
        raise AdmissionDenied(
            "EFFECTIVE_POLICY_SNAPSHOT_REQUIRED",
            "The Runtime Bundle must be republished with effective-policy admission.",
        )
    compiler_value = facts.manifest.get("compiler")
    if not isinstance(compiler_value, Mapping):
        raise AdmissionDenied(
            "BUNDLE_COMPILER_IDENTITY_MISMATCH",
            "The Runtime Bundle compiler identity is inconsistent.",
        )
    compiler = cast(Mapping[str, object], compiler_value)
    if compiler != {
        "name": facts.compiler_name,
        "version": facts.compiler_version,
    }:
        raise AdmissionDenied(
            "BUNDLE_COMPILER_IDENTITY_MISMATCH",
            "The Runtime Bundle compiler identity is inconsistent.",
        )
    security_value = facts.manifest.get("security")
    if not isinstance(security_value, Mapping):
        raise AdmissionDenied(
            "BUNDLE_SECURITY_FACTS_INVALID",
            "The Runtime Bundle security facts are incomplete.",
        )
    security = cast(Mapping[str, object], security_value)
    if set(security) != {
        "permission_policy_hash",
        "sandbox_policy_hash",
        "secret_refs",
    }:
        raise AdmissionDenied(
            "BUNDLE_SECURITY_FACTS_INVALID",
            "The Runtime Bundle security facts are incomplete.",
        )
    for field_name in ("permission_policy_hash", "sandbox_policy_hash"):
        value = security.get(field_name)
        if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
            raise AdmissionDenied(
                "BUNDLE_SECURITY_FACTS_INVALID",
                "The Runtime Bundle policy hashes are invalid.",
            )
    secret_refs_value = security.get("secret_refs")
    if not isinstance(secret_refs_value, list):
        raise AdmissionDenied(
            "BUNDLE_SECURITY_FACTS_INVALID",
            "The Runtime Bundle Secret references are invalid.",
        )
    secret_refs = cast(list[object], secret_refs_value)
    if (
        len(secret_refs) > 100
        or any(
            not isinstance(value, str) or not value.startswith("secret://")
            for value in secret_refs
        )
        or secret_refs != sorted(secret_refs, key=str)
        or len(secret_refs) != len(set(secret_refs))
    ):
        raise AdmissionDenied(
            "BUNDLE_SECURITY_FACTS_INVALID",
            "The Runtime Bundle Secret references are invalid.",
        )

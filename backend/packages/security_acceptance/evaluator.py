"""Fail-closed supply-chain and Kubernetes Sandbox acceptance checks."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import yaml

from packages.security_acceptance.config import (
    RiskException,
    SecurityAcceptancePlan,
    SupplyChainEvidence,
    load_supply_chain_evidence,
)
from packages.security_acceptance.report import SecurityAcceptanceReport

_SANDBOX_LABEL = "agent-platform.io/sandbox"
_REQUIRED_RESOURCE_KEYS = frozenset({"cpu", "memory", "ephemeral-storage"})
_SENSITIVE_ENV_PARTS = ("credential", "key", "password", "secret", "token")


def evaluate_plan(
    plan: SecurityAcceptancePlan,
    *,
    base_directory: Path,
    now: datetime | None = None,
) -> SecurityAcceptanceReport:
    instant = now or datetime.now(UTC)
    evidence_paths = [
        _resolve_plan_path(base_directory, relative_path)
        for relative_path in plan.supply_chain_evidence
    ]
    sandbox_manifest_path = _resolve_plan_path(base_directory, plan.sandbox_manifest)
    network_policy_manifest_path = _resolve_plan_path(
        base_directory, plan.network_policy_manifest
    )
    report = SecurityAcceptanceReport.new(
        plan_id=plan.plan_id, mode=plan.mode, metadata=plan.metadata
    )
    accepted_image_digests: set[str] = set()
    for path in evidence_paths:
        evidence = load_supply_chain_evidence(path)
        if evidence.subject_type == "image":
            accepted_image_digests.add(evidence.subject_digest)
        _evaluate_supply_chain(
            report, evidence, instant=instant, production=plan.mode == "production"
        )
    sandbox_documents = _load_documents(sandbox_manifest_path)
    network_documents = _load_documents(network_policy_manifest_path)
    _evaluate_sandbox_manifest(
        report,
        sandbox_documents,
        expected_runtime_class=plan.expected_runtime_class,
        accepted_image_digests=accepted_image_digests,
        production=plan.mode == "production",
    )
    _evaluate_network_policy(
        report,
        network_documents,
        expected_network_mode=plan.expected_network_mode,
    )
    report["follow_up"] = [
        "Execute real image signature/provenance verification against the registry.",
        "Execute hostile workload, escape, secret leakage and resource exhaustion tests on the production RuntimeClass.",
        "Attach CI SBOM, vulnerability, license, secret and malicious-code reports to the final release evidence.",
    ]
    report.finish()
    return report


def _resolve_plan_path(base_directory: Path, relative_path: str) -> Path:
    candidate = (base_directory / relative_path).resolve()
    base = base_directory.resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError("security acceptance paths must stay under the plan directory")
    return candidate


def _load_documents(path: Path) -> list[dict[str, Any]]:
    documents = [
        item for item in yaml.safe_load_all(path.read_text(encoding="utf-8")) if item
    ]
    if not documents or not all(isinstance(item, dict) for item in documents):
        raise ValueError(f"{path.name} must contain Kubernetes object mappings")
    return cast(list[dict[str, Any]], documents)


def _evaluate_supply_chain(
    report: SecurityAcceptanceReport,
    evidence: SupplyChainEvidence,
    *,
    instant: datetime,
    production: bool,
) -> None:
    prefix = f"supply_chain.{evidence.subject_name}"
    if production and evidence.risk_exceptions:
        report.add_check(
            name=f"{prefix}.production_risk_exceptions",
            passed=False,
            message="production security acceptance does not allow high-risk vulnerability exceptions",
        )
    scans = {
        "vulnerability": evidence.vulnerability_scan,
        "license": evidence.license_scan,
        "secret": evidence.secret_scan,
        "malicious_code": evidence.malicious_code_scan,
    }
    for name, scan in scans.items():
        report.add_check(
            name=f"{prefix}.{name}_scan",
            passed=scan.status == "PASSED",
            message=f"{name} scan status is {scan.status}",
        )
    vulnerability = evidence.vulnerability_scan
    findings = {
        "critical_vulnerability": vulnerability.critical_count,
        "high_vulnerability": vulnerability.high_count,
        "known_exploitable_vulnerability": vulnerability.known_exploitable_count,
    }
    for finding, count in findings.items():
        exception = (
            None
            if production
            else _active_exception(evidence.risk_exceptions, finding, instant)
        )
        passed = count == 0 or exception is not None
        suffix = (
            f"; accepted by {exception.accepted_by} until {exception.expires_at.isoformat()}"
            if exception
            else ""
        )
        report.add_check(
            name=f"{prefix}.{finding}",
            passed=passed,
            message=f"{finding} count is {count}{suffix}",
        )
    for name, verification in {
        "signature": evidence.signature,
        "provenance": evidence.provenance,
    }.items():
        report.add_check(
            name=f"{prefix}.{name}",
            passed=verification.status == "VERIFIED",
            message=f"{name} status is {verification.status}",
        )
    if production:
        serialized = evidence.model_dump_json()
        report.add_check(
            name=f"{prefix}.production_evidence",
            passed="example.invalid" not in serialized
            and "replace-with" not in serialized,
            message="production evidence must not contain placeholder locations or identities",
        )


def _active_exception(
    exceptions: list[RiskException], finding: str, instant: datetime
) -> RiskException | None:
    for exception in exceptions:
        if exception.finding != finding:
            continue
        offset = exception.expires_at.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            continue
        if exception.expires_at > instant:
            return exception
    return None


def _evaluate_sandbox_manifest(
    report: SecurityAcceptanceReport,
    documents: list[dict[str, Any]],
    *,
    expected_runtime_class: str,
    accepted_image_digests: set[str],
    production: bool,
) -> None:
    pod = next((item for item in documents if item.get("kind") == "Pod"), None)
    report.add_check(
        name="sandbox.pod_present",
        passed=pod is not None,
        message="sandbox manifest must contain one Pod",
    )
    if pod is None:
        return
    metadata = _mapping(pod, "metadata")
    labels = _mapping(metadata, "labels")
    annotations = _mapping(metadata, "annotations")
    spec = _mapping(pod, "spec")
    report.add_check(
        name="sandbox.label",
        passed=labels.get(_SANDBOX_LABEL) == "true",
        message=f"Pod label {_SANDBOX_LABEL}=true is required",
    )
    report.add_check(
        name="sandbox.runtime_class",
        passed=spec.get("runtimeClassName") == expected_runtime_class,
        message=f"runtimeClassName must be {expected_runtime_class}",
    )
    namespace_safe = all(
        spec.get(name) is not True for name in ("hostNetwork", "hostPID", "hostIPC")
    )
    report.add_check(
        name="sandbox.host_namespaces",
        passed=namespace_safe,
        message="hostNetwork, hostPID and hostIPC must be false",
    )
    service_account = spec.get("serviceAccountName")
    report.add_check(
        name="sandbox.service_account",
        passed=isinstance(service_account, str)
        and service_account not in {"", "default"}
        and spec.get("automountServiceAccountToken") is False,
        message="a dedicated ServiceAccount with token automount disabled is required",
    )
    service_account_document = next(
        (
            item
            for item in documents
            if item.get("kind") == "ServiceAccount"
            and _mapping(item, "metadata").get("name") == service_account
            and _mapping(item, "metadata").get("namespace") == metadata.get("namespace")
        ),
        None,
    )
    report.add_check(
        name="sandbox.service_account_manifest",
        passed=service_account_document is not None
        and service_account_document.get("automountServiceAccountToken") is False,
        message="the dedicated ServiceAccount manifest must disable token automount",
    )
    report.add_check(
        name="sandbox.enable_service_links",
        passed=spec.get("enableServiceLinks") is False,
        message="enableServiceLinks must be false to avoid ambient service data",
    )
    pod_security = _mapping(spec, "securityContext")
    report.add_check(
        name="sandbox.pod_security_context",
        passed=pod_security.get("runAsNonRoot") is True
        and _mapping(pod_security, "seccompProfile").get("type") == "RuntimeDefault",
        message="Pod must run as non-root with RuntimeDefault seccomp",
    )
    report.add_check(
        name="sandbox.lifecycle",
        passed=spec.get("restartPolicy") == "Never"
        and isinstance(spec.get("terminationGracePeriodSeconds"), int)
        and spec["terminationGracePeriodSeconds"] > 0,
        message="Sandbox Pod must not restart and must define a positive termination grace period",
    )
    volumes = _object_list(spec.get("volumes"))
    volume_safe = all(
        isinstance(volume, dict)
        and "hostPath" not in volume
        and "persistentVolumeClaim" not in volume
        for volume in volumes
    )
    report.add_check(
        name="sandbox.volumes",
        passed=volume_safe,
        message="Sandbox volumes must not use hostPath or arbitrary PVCs",
    )
    policy_annotations = (
        _sha256(annotations.get("agent-platform.io/policy-hash"))
        and _positive_int(annotations.get("agent-platform.io/pids-limit"))
        and _positive_int(annotations.get("agent-platform.io/timeout-seconds"))
    )
    report.add_check(
        name="sandbox.policy_annotations",
        passed=policy_annotations,
        message="policy hash, PID limit and timeout annotations are required",
    )
    containers = _object_list(spec.get("containers"))
    if not containers:
        report.add_check(
            name="sandbox.containers",
            passed=False,
            message="Sandbox Pod must contain at least one container",
        )
        return
    for index, item in enumerate(containers):
        container = cast(dict[str, Any], item) if isinstance(item, dict) else {}
        _evaluate_container(
            report,
            container,
            index=index,
            accepted_image_digests=accepted_image_digests,
            production=production,
        )


def _evaluate_container(
    report: SecurityAcceptanceReport,
    container: dict[str, Any],
    *,
    index: int,
    accepted_image_digests: set[str],
    production: bool,
) -> None:
    prefix = f"sandbox.container.{index}"
    image = container.get("image")
    image_digest = _image_digest(image)
    report.add_check(
        name=f"{prefix}.image_digest",
        passed=image_digest is not None,
        message="container image must use an immutable registry digest",
    )
    report.add_check(
        name=f"{prefix}.supply_chain_binding",
        passed=image_digest in accepted_image_digests,
        message="container image digest must match supplied image security evidence",
    )
    if production:
        report.add_check(
            name=f"{prefix}.production_image",
            passed=isinstance(image, str) and not _contains_placeholder(image),
            message="production container image must not use a placeholder registry or identity",
        )
    security = _mapping(container, "securityContext")
    capabilities = _mapping(security, "capabilities")
    dropped = _object_list(capabilities.get("drop"))
    security_ok = (
        security.get("runAsNonRoot") is True
        and security.get("allowPrivilegeEscalation") is False
        and security.get("readOnlyRootFilesystem") is True
        and security.get("privileged") is not True
        and "ALL" in dropped
    )
    report.add_check(
        name=f"{prefix}.security_context",
        passed=security_ok,
        message="non-root, no privilege escalation, read-only root and drop ALL are required",
    )
    report.add_check(
        name=f"{prefix}.apparmor",
        passed=_mapping(security, "appArmorProfile").get("type") == "RuntimeDefault",
        message="container AppArmor profile must use RuntimeDefault",
    )
    resources = _mapping(container, "resources")
    requests = _mapping(resources, "requests")
    limits = _mapping(resources, "limits")
    report.add_check(
        name=f"{prefix}.resources",
        passed=_REQUIRED_RESOURCE_KEYS <= requests.keys()
        and _REQUIRED_RESOURCE_KEYS <= limits.keys(),
        message="CPU, memory and ephemeral-storage requests/limits are required",
    )
    command = _object_list(container.get("command"))
    shell_free = not any(
        str(part).rsplit("/", 1)[-1] in {"bash", "sh", "zsh"} for part in command
    )
    report.add_check(
        name=f"{prefix}.shell",
        passed=shell_free,
        message="Sandbox entrypoint must not invoke an unrestricted shell",
    )
    environment = _object_list(container.get("env"))
    env_from = _object_list(container.get("envFrom"))
    environment_safe = not env_from
    for entry in environment:
        if not isinstance(entry, dict):
            environment_safe = False
            continue
        typed_entry = cast(dict[str, Any], entry)
        name = str(typed_entry.get("name", "")).lower()
        value_from = typed_entry.get("valueFrom")
        if any(part in name for part in _SENSITIVE_ENV_PARTS) or value_from:
            environment_safe = False
    report.add_check(
        name=f"{prefix}.environment",
        passed=environment_safe,
        message="Secret, credential and capability values must not be injected as environment variables",
    )
    mounts = _object_list(container.get("volumeMounts"))
    mount_safe = all(
        isinstance(mount, dict)
        and cast(dict[str, Any], mount).get("mountPath")
        not in {"/var/run/docker.sock", "/run/containerd/containerd.sock"}
        and cast(dict[str, Any], mount).get("mountPropagation") != "Bidirectional"
        for mount in mounts
    )
    report.add_check(
        name=f"{prefix}.mounts",
        passed=mount_safe,
        message="Docker/containerd sockets and bidirectional mounts are forbidden",
    )


def _evaluate_network_policy(
    report: SecurityAcceptanceReport,
    documents: list[dict[str, Any]],
    *,
    expected_network_mode: str,
) -> None:
    policy = next(
        (item for item in documents if item.get("kind") == "NetworkPolicy"), None
    )
    report.add_check(
        name="sandbox.network_policy_present",
        passed=policy is not None,
        message="Sandbox NetworkPolicy is required",
    )
    if policy is None:
        return
    spec = _mapping(policy, "spec")
    labels = _mapping(_mapping(spec, "podSelector"), "matchLabels")
    types = _object_list(spec.get("policyTypes"))
    base_ok = (
        labels.get(_SANDBOX_LABEL) == "true"
        and {"Ingress", "Egress"} <= set(types)
        and spec.get("ingress") == []
    )
    report.add_check(
        name="sandbox.network_policy_base",
        passed=base_ok,
        message="NetworkPolicy must select Sandbox Pods and default-deny ingress",
    )
    egress = spec.get("egress")
    if expected_network_mode == "none":
        passed = egress == []
        message = "network mode none requires default-deny egress"
    else:
        passed = _proxy_only_egress(egress)
        message = "proxy_allowlist requires selector-based egress only to the controlled proxy"
    report.add_check(
        name="sandbox.network_policy_egress",
        passed=passed,
        message=message,
    )


def _proxy_only_egress(value: object) -> bool:
    if not isinstance(value, list) or not value:
        return False
    rules = cast(list[object], value)
    for rule in rules:
        if not isinstance(rule, dict):
            return False
        typed_rule = cast(dict[str, Any], rule)
        peers = _object_list(typed_rule.get("to"))
        ports = _object_list(typed_rule.get("ports"))
        if not peers or not ports:
            return False
        for peer in peers:
            if not isinstance(peer, dict) or "ipBlock" in peer:
                return False
            if "namespaceSelector" not in peer or "podSelector" not in peer:
                return False
            namespace_labels = _mapping(
                _mapping(cast(dict[str, Any], peer), "namespaceSelector"),
                "matchLabels",
            )
            pod_labels = _mapping(
                _mapping(cast(dict[str, Any], peer), "podSelector"), "matchLabels"
            )
            if not namespace_labels or not pod_labels:
                return False
    return True


def _mapping(value: dict[str, Any], name: str) -> dict[str, Any]:
    item = value.get(name)
    return cast(dict[str, Any], item) if isinstance(item, dict) else {}


def _object_list(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def _sha256(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(
        character in "0123456789abcdef" for character in digest
    )


def _image_digest(value: object) -> str | None:
    if not isinstance(value, str) or value.count("@") != 1:
        return None
    digest = value.rsplit("@", 1)[1]
    return digest if _sha256(digest) else None


def _contains_placeholder(value: str) -> bool:
    lowered = value.lower()
    return "example.invalid" in lowered or "replace-with" in lowered


def _positive_int(value: object) -> bool:
    if not isinstance(value, str) or not value.isdigit():
        return False
    return int(value) > 0

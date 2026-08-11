"""Fail-closed Admission Controller for immutable Runtime Bundles."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from packages.domain.bundles import BUNDLE_COMPILER_NAME, BUNDLE_COMPILER_VERSION

_HASH_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


class AdmissionDenied(ValueError):
    """A stable internal denial that callers map to their frozen API errors."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


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

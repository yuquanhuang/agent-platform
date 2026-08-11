"""Runtime Bundle Admission Controller tests."""

import pytest

from packages.application.policy import (
    AdmissionDenied,
    RuntimeBundleAdmissionFacts,
    admit_runtime_bundle,
)
from packages.domain.bundles import BUNDLE_COMPILER_NAME, BUNDLE_COMPILER_VERSION

HASH = "sha256:" + "a" * 64


def facts(*, version: str = BUNDLE_COMPILER_VERSION, scan: str = "PASSED"):
    return RuntimeBundleAdmissionFacts(
        compiler_name=BUNDLE_COMPILER_NAME,
        compiler_version=version,
        scan_status=scan,
        manifest={
            "compiler": {"name": BUNDLE_COMPILER_NAME, "version": version},
            "security": {
                "permission_policy_hash": HASH,
                "sandbox_policy_hash": HASH,
                "secret_refs": [],
            },
        },
    )


def test_current_effective_policy_bundle_is_admitted() -> None:
    admit_runtime_bundle(facts())


@pytest.mark.parametrize(
    ("candidate", "code"),
    [
        (facts(version="1.0.0"), "EFFECTIVE_POLICY_SNAPSHOT_REQUIRED"),
        (facts(scan="PENDING"), "BUNDLE_SCAN_NOT_PASSED"),
    ],
)
def test_legacy_or_unscanned_bundle_is_denied(
    candidate: RuntimeBundleAdmissionFacts, code: str
) -> None:
    with pytest.raises(AdmissionDenied) as error:
        admit_runtime_bundle(candidate)

    assert error.value.code == code

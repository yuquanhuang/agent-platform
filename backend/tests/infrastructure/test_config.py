"""Process configuration validation tests."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.contracts.public import EXPECTED_CONTRACT_BASELINE_ID
from packages.infrastructure.public import AppSettings, AuthMode, DeploymentEnvironment


def test_local_defaults_are_safe_and_match_frozen_baseline() -> None:
    settings = AppSettings.model_validate({})

    assert settings.env is DeploymentEnvironment.LOCAL
    assert settings.auth_mode is AuthMode.MOCK
    assert settings.contract_baseline_id == EXPECTED_CONTRACT_BASELINE_ID


def test_settings_load_ap_prefixed_environment_variables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AP_SERVICE_NAME", "api-from-env")

    settings = AppSettings()

    assert settings.service_name == "api-from-env"


def test_mock_auth_is_rejected_in_production() -> None:
    with pytest.raises(ValidationError, match="allowed only in local/test"):
        AppSettings.model_validate({"env": DeploymentEnvironment.PRODUCTION})


def test_oidc_mode_requires_issuer_client_and_secret_reference() -> None:
    with pytest.raises(ValidationError, match="OIDC mode requires"):
        AppSettings.model_validate({"auth_mode": AuthMode.OIDC})


def test_contract_baseline_mismatch_fails_startup_configuration() -> None:
    with pytest.raises(ValidationError, match="does not match"):
        AppSettings.model_validate({"contract_baseline_id": "unexpected-baseline"})

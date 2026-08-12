"""Process configuration validation tests."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.contracts.public import EXPECTED_CONTRACT_BASELINE_ID
from packages.infrastructure.public import (
    AppSettings,
    AuthMode,
    DeploymentEnvironment,
    SecretBackendKind,
)


def test_local_defaults_are_safe_and_match_frozen_baseline() -> None:
    settings = AppSettings.model_validate({})

    assert settings.env is DeploymentEnvironment.LOCAL
    assert settings.auth_mode is AuthMode.MOCK
    assert settings.secret_backend is SecretBackendKind.ENV
    assert settings.contract_baseline_id == EXPECTED_CONTRACT_BASELINE_ID
    assert settings.sse_page_size == 200
    assert settings.sse_heartbeat_seconds == 15.0
    assert settings.sse_poll_interval_seconds == 1.0
    assert settings.sse_send_timeout_seconds == 15.0
    assert settings.sandbox_provider_timeout_seconds == 60.0
    assert settings.artifact_public_origins == ()
    assert settings.run_max_nonterminal_per_tenant is None
    assert settings.run_max_nonterminal_codex is None
    assert settings.internal_service_token_audience == "sandbox-manager"
    assert settings.internal_service_token_ttl_seconds == 60
    assert settings.sandbox_manager_request_timeout_seconds == 10.0


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


def test_mock_active_tenant_requires_membership_version() -> None:
    with pytest.raises(ValidationError, match="AP_MOCK_MEMBERSHIP_VERSION"):
        AppSettings.model_validate(
            {"mock_active_tenant_id": "11111111-1111-4111-8111-111111111111"}
        )


def test_metrics_networks_must_be_valid_and_non_empty() -> None:
    with pytest.raises(ValidationError, match="valid CIDRs"):
        AppSettings.model_validate({"metrics_allowed_networks": ["not-a-cidr"]})
    with pytest.raises(ValidationError, match="must not be empty"):
        AppSettings.model_validate({"metrics_allowed_networks": []})


def test_artifact_public_origins_load_as_explicit_tuple() -> None:
    settings = AppSettings.model_validate(
        {"artifact_public_origins": ["https://artifacts.example.test"]}
    )

    assert settings.artifact_public_origins == ("https://artifacts.example.test",)


def test_run_capacity_limits_are_positive_and_bounded() -> None:
    settings = AppSettings.model_validate(
        {
            "run_max_nonterminal_per_tenant": 100,
            "run_max_nonterminal_per_user": 10,
            "run_max_nonterminal_per_agent": 50,
            "run_max_nonterminal_agentscope": 100,
            "run_max_nonterminal_codex": 20,
        }
    )

    assert settings.run_max_nonterminal_per_tenant == 100
    assert settings.run_max_nonterminal_codex == 20

    with pytest.raises(ValidationError):
        AppSettings.model_validate({"run_max_nonterminal_per_user": 0})


def test_internal_service_token_limits_are_bounded() -> None:
    with pytest.raises(ValidationError):
        AppSettings.model_validate({"internal_service_token_ttl_seconds": 10})
    with pytest.raises(ValidationError):
        AppSettings.model_validate({"internal_service_token_clock_skew_seconds": 60})
    with pytest.raises(ValidationError):
        AppSettings.model_validate({"internal_service_token_key_id": "invalid key"})


def test_sandbox_manager_requires_https_outside_local_test() -> None:
    with pytest.raises(ValidationError, match="must use HTTPS"):
        AppSettings.model_validate(
            {
                "env": "production",
                "auth_mode": "oidc",
                "oidc_issuer": "https://issuer.example.test",
                "oidc_client_id": "agent-platform",
                "oidc_client_secret_ref": ("secret://env/AP_SECRET_OIDC_CLIENT_SECRET"),
                "sandbox_manager_base_url": ("http://sandbox-manager.example.test"),
            }
        )

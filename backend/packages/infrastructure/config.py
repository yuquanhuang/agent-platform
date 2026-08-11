"""Strongly typed process configuration."""

import ipaddress
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import AnyHttpUrl, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from packages.contracts.public import EXPECTED_CONTRACT_BASELINE_ID

SecretReference = Annotated[
    str,
    Field(pattern=r"^[a-z][a-z0-9+.-]*://\S+$"),
]


class DeploymentEnvironment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class AuthMode(StrEnum):
    MOCK = "mock"
    OIDC = "oidc"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class AppSettings(BaseSettings):
    """Validated configuration shared by backend process entrypoints."""

    model_config = SettingsConfigDict(
        env_prefix="AP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        validate_default=True,
    )

    env: DeploymentEnvironment = DeploymentEnvironment.LOCAL
    service_name: Annotated[str, Field(min_length=1, max_length=128)] = "api"
    public_base_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8000")
    auth_mode: AuthMode = AuthMode.MOCK
    database_dsn_ref: SecretReference | None = None
    redis_dsn_ref: SecretReference | None = None
    object_storage_endpoint: AnyHttpUrl | None = None
    object_storage_bucket: (
        Annotated[str, Field(min_length=1, max_length=255)] | None
    ) = None
    object_storage_credential_ref: SecretReference | None = None
    artifact_public_origins: tuple[str, ...] = ()
    temporal_address: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    temporal_namespace: Annotated[str, Field(min_length=1, max_length=255)] | None = (
        None
    )
    temporal_connect_timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 10.0
    worker_shutdown_grace_seconds: Annotated[float, Field(ge=0, le=300)] = 30.0
    outbox_batch_size: Annotated[int, Field(ge=1, le=500)] = 50
    outbox_max_attempts: Annotated[int, Field(ge=1, le=100)] = 10
    sse_page_size: Annotated[int, Field(ge=1, le=200)] = 200
    sse_heartbeat_seconds: Annotated[float, Field(gt=0, le=300)] = 15.0
    sse_poll_interval_seconds: Annotated[float, Field(gt=0, le=60)] = 1.0
    sse_send_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 15.0
    sandbox_provider_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 60.0
    metrics_allowed_networks: tuple[str, ...] = ("127.0.0.1/32", "::1/128")
    oidc_issuer: AnyHttpUrl | None = None
    oidc_client_id: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    oidc_client_secret_ref: SecretReference | None = None
    secret_backend: Annotated[str, Field(min_length=1, max_length=64)] = "local"
    otel_exporter_otlp_endpoint: AnyHttpUrl | None = None
    log_level: LogLevel = LogLevel.INFO
    contract_baseline_id: str = EXPECTED_CONTRACT_BASELINE_ID
    api_host: Annotated[str, Field(min_length=1, max_length=255)] = "127.0.0.1"
    api_port: Annotated[int, Field(ge=1, le=65535)] = 8000
    mock_identity_issuer: AnyHttpUrl = AnyHttpUrl("https://mock.agent-platform.test")
    mock_external_subject: Annotated[str, Field(min_length=1, max_length=255)] = (
        "mock-platform-admin"
    )
    mock_display_name: Annotated[str, Field(min_length=1, max_length=100)] = (
        "Mock Platform Admin"
    )
    mock_email: Annotated[str, Field(min_length=3, max_length=320)] | None = (
        "mock-admin@example.test"
    )
    mock_platform_roles: tuple[Literal["platform_admin"], ...] = ("platform_admin",)
    mock_active_tenant_id: (
        Annotated[
            str,
            Field(
                pattern=(
                    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
                    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
                )
            ),
        ]
        | None
    ) = None
    mock_membership_version: Annotated[int, Field(ge=1)] | None = None

    @field_validator("metrics_allowed_networks")
    @classmethod
    def validate_metrics_networks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("AP_METRICS_ALLOWED_NETWORKS must not be empty")
        for network in value:
            try:
                ipaddress.ip_network(network, strict=False)
            except ValueError as exc:
                raise ValueError(
                    "AP_METRICS_ALLOWED_NETWORKS must contain valid CIDRs"
                ) from exc
        return value

    @model_validator(mode="after")
    def validate_environment_security(self) -> Self:
        if self.contract_baseline_id != EXPECTED_CONTRACT_BASELINE_ID:
            raise ValueError(
                "AP_CONTRACT_BASELINE_ID does not match the backend contract baseline"
            )

        if (
            self.env
            in {
                DeploymentEnvironment.STAGING,
                DeploymentEnvironment.PRODUCTION,
            }
            and self.auth_mode is AuthMode.MOCK
        ):
            raise ValueError("AP_AUTH_MODE=mock is allowed only in local/test")

        if self.auth_mode is AuthMode.OIDC:
            missing = [
                name
                for name, value in {
                    "AP_OIDC_ISSUER": self.oidc_issuer,
                    "AP_OIDC_CLIENT_ID": self.oidc_client_id,
                    "AP_OIDC_CLIENT_SECRET_REF": self.oidc_client_secret_ref,
                }.items()
                if value is None
            ]
            if missing:
                raise ValueError(f"OIDC mode requires: {', '.join(missing)}")
        if (
            self.auth_mode is AuthMode.MOCK
            and self.mock_active_tenant_id is not None
            and self.mock_membership_version is None
        ):
            raise ValueError(
                "AP_MOCK_MEMBERSHIP_VERSION is required when "
                "AP_MOCK_ACTIVE_TENANT_ID is configured"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    return AppSettings()

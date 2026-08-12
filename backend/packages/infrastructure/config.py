"""Strongly typed process configuration."""

import ipaddress
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal, Self
from uuid import UUID

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


class SecretBackendKind(StrEnum):
    ENV = "env"
    VAULT = "vault"


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
    service_subject_id: UUID | None = None
    public_base_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8000")
    auth_mode: AuthMode = AuthMode.MOCK
    database_dsn_ref: SecretReference | None = None
    redis_dsn_ref: SecretReference | None = None
    object_storage_endpoint: AnyHttpUrl | None = None
    object_storage_bucket: (
        Annotated[str, Field(min_length=1, max_length=255)] | None
    ) = None
    object_storage_credential_ref: SecretReference | None = None
    object_storage_region: (
        Annotated[str, Field(min_length=1, max_length=128)] | None
    ) = None
    object_storage_connect_timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 5.0
    object_storage_read_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 30.0
    object_storage_download_chunk_bytes: Annotated[
        int, Field(ge=65_536, le=8_388_608)
    ] = 1_048_576
    object_storage_max_concurrent_downloads: Annotated[int, Field(ge=1, le=10_000)] = 32
    object_storage_download_acquire_timeout_seconds: Annotated[
        float, Field(gt=0, le=60)
    ] = 1.0
    artifact_download_revocation_check_interval_seconds: Annotated[
        float, Field(gt=0, le=60)
    ] = 5.0
    artifact_download_max_duration_seconds: Annotated[float, Field(gt=0, le=86_400)] = (
        3600.0
    )
    artifact_public_origins: tuple[str, ...] = ()
    temporal_address: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    temporal_namespace: Annotated[str, Field(min_length=1, max_length=255)] | None = (
        None
    )
    temporal_connect_timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 10.0
    sandbox_manager_base_url: AnyHttpUrl | None = None
    sandbox_manager_request_timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 10.0
    sandbox_manager_allowed_subject_ids: tuple[UUID, ...] = ()
    internal_service_token_issuer: AnyHttpUrl | None = None
    internal_service_token_audience: Annotated[
        str, Field(min_length=1, max_length=128)
    ] = "sandbox-manager"
    internal_service_token_key_id: Annotated[
        str, Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    ] = "v1"
    internal_service_token_signing_key_ref: SecretReference | None = None
    internal_service_token_verification_key_ref: SecretReference | None = None
    internal_service_token_ttl_seconds: Annotated[int, Field(ge=30, le=300)] = 60
    internal_service_token_clock_skew_seconds: Annotated[int, Field(ge=0, le=30)] = 5
    execution_ticket_key_ref: SecretReference | None = None
    worker_shutdown_grace_seconds: Annotated[float, Field(ge=0, le=300)] = 30.0
    outbox_batch_size: Annotated[int, Field(ge=1, le=500)] = 50
    outbox_max_attempts: Annotated[int, Field(ge=1, le=100)] = 10
    worker_tenant_limit: Annotated[int, Field(ge=1, le=500)] = 100
    event_worker_poll_interval_seconds: Annotated[float, Field(gt=0, le=60)] = 1.0
    reconciliation_worker_poll_interval_seconds: Annotated[
        float, Field(gt=0, le=3600)
    ] = 30.0
    sse_page_size: Annotated[int, Field(ge=1, le=200)] = 200
    sse_heartbeat_seconds: Annotated[float, Field(gt=0, le=300)] = 15.0
    sse_poll_interval_seconds: Annotated[float, Field(gt=0, le=60)] = 1.0
    sse_send_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 15.0
    sandbox_provider_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 60.0
    run_max_nonterminal_per_tenant: Annotated[int, Field(ge=1, le=1_000_000)] | None = (
        None
    )
    run_max_nonterminal_per_user: Annotated[int, Field(ge=1, le=1_000_000)] | None = (
        None
    )
    run_max_nonterminal_per_agent: Annotated[int, Field(ge=1, le=1_000_000)] | None = (
        None
    )
    run_max_nonterminal_agentscope: Annotated[int, Field(ge=1, le=1_000_000)] | None = (
        None
    )
    run_max_nonterminal_codex: Annotated[int, Field(ge=1, le=1_000_000)] | None = None
    metrics_allowed_networks: tuple[str, ...] = ("127.0.0.1/32", "::1/128")
    oidc_issuer: AnyHttpUrl | None = None
    oidc_client_id: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    oidc_client_secret_ref: SecretReference | None = None
    runtime_checkpoint_key_ref: SecretReference | None = None
    runtime_checkpoint_retention_seconds: Annotated[int, Field(ge=300, le=604_800)] = (
        86_400
    )
    runtime_checkpoint_max_state_bytes: Annotated[int, Field(ge=1, le=104_857_600)] = (
        10_485_760
    )
    secret_backend: SecretBackendKind = SecretBackendKind.ENV
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
        if (
            self.env
            in {
                DeploymentEnvironment.STAGING,
                DeploymentEnvironment.PRODUCTION,
            }
            and self.sandbox_manager_base_url is not None
            and self.sandbox_manager_base_url.scheme != "https"
        ):
            raise ValueError(
                "AP_SANDBOX_MANAGER_BASE_URL must use HTTPS in staging/production"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    return AppSettings()

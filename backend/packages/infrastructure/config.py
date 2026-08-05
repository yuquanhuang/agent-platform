"""Strongly typed process configuration."""

from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Self

from pydantic import AnyHttpUrl, Field, model_validator
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
    temporal_address: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    temporal_namespace: Annotated[str, Field(min_length=1, max_length=255)] | None = (
        None
    )
    oidc_issuer: AnyHttpUrl | None = None
    oidc_client_id: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    oidc_client_secret_ref: SecretReference | None = None
    secret_backend: Annotated[str, Field(min_length=1, max_length=64)] = "local"
    otel_exporter_otlp_endpoint: AnyHttpUrl | None = None
    log_level: LogLevel = LogLevel.INFO
    contract_baseline_id: str = EXPECTED_CONTRACT_BASELINE_ID
    api_host: Annotated[str, Field(min_length=1, max_length=255)] = "127.0.0.1"
    api_port: Annotated[int, Field(ge=1, le=65535)] = 8000

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
        return self


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    return AppSettings()

"""Authentication inputs shared by API and application layers."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

UUID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)


class AuthenticatedPrincipal(BaseModel):
    """Server-derived identity claims before authoritative database lookup."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    identity_issuer: str = Field(min_length=1, max_length=255)
    external_subject: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=100)
    email: str | None = Field(default=None, max_length=320)
    platform_roles: frozenset[str] = Field(default_factory=frozenset)
    active_tenant_id: str | None = Field(default=None, pattern=UUID_PATTERN)
    membership_version: int | None = Field(default=None, ge=1)
    auth_time: datetime

    @field_validator("auth_time")
    @classmethod
    def validate_utc_auth_time(cls, value: datetime) -> datetime:
        offset = value.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError("auth_time must be timezone-aware UTC")
        return value


class IdentityProvider:
    """Structural base for authentication adapters."""

    def authenticate(self, authorization: str | None) -> AuthenticatedPrincipal:
        raise NotImplementedError

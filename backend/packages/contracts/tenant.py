"""Tenant-scoped identity context shared across backend layers."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SubjectType(StrEnum):
    """Supported authenticated subject categories."""

    USER = "user"
    SERVICE = "service"


class TenantContext(BaseModel):
    """Authoritative tenant and subject context for one request or activity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    subject_type: SubjectType
    subject_id: str
    membership_version: int | None = Field(default=None, ge=1)
    auth_time: datetime
    request_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)

    @field_validator("tenant_id", "subject_id")
    @classmethod
    def validate_uuid_identifier(cls, value: str) -> str:
        try:
            UUID(value)
        except ValueError as exc:
            raise ValueError("must be a valid UUID") from exc
        return value

    @field_validator("auth_time")
    @classmethod
    def validate_utc_auth_time(cls, value: datetime) -> datetime:
        offset = value.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError("auth_time must be timezone-aware UTC")
        return value

    @model_validator(mode="after")
    def validate_subject_membership(self) -> "TenantContext":
        if self.subject_type is SubjectType.USER and self.membership_version is None:
            raise ValueError("user TenantContext requires membership_version")
        return self

"""TenantContext contract validation tests."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from packages.contracts.public import SubjectType, TenantContext

TENANT_ID = "11111111-1111-4111-8111-111111111111"
SUBJECT_ID = "22222222-2222-4222-8222-222222222222"


def valid_context_values() -> dict[str, object]:
    return {
        "tenant_id": TENANT_ID,
        "subject_type": SubjectType.USER,
        "subject_id": SUBJECT_ID,
        "membership_version": 3,
        "auth_time": datetime(2026, 8, 5, tzinfo=UTC),
        "request_id": "req-1",
        "trace_id": "trace-1",
    }


def test_tenant_context_accepts_authoritative_user_context() -> None:
    context = TenantContext.model_validate(valid_context_values())

    assert context.tenant_id == TENANT_ID
    assert context.subject_type is SubjectType.USER
    assert context.membership_version == 3


@pytest.mark.parametrize("field", ["tenant_id", "subject_id"])
def test_tenant_context_rejects_invalid_uuid(field: str) -> None:
    values = valid_context_values()
    values[field] = "not-a-uuid"

    with pytest.raises(ValidationError, match="valid UUID"):
        TenantContext.model_validate(values)


def test_user_context_requires_membership_version() -> None:
    values = valid_context_values()
    values["membership_version"] = None

    with pytest.raises(ValidationError, match="requires membership_version"):
        TenantContext.model_validate(values)


def test_service_context_can_omit_membership_version() -> None:
    values = valid_context_values()
    values["subject_type"] = SubjectType.SERVICE
    values["membership_version"] = None

    context = TenantContext.model_validate(values)

    assert context.membership_version is None


def test_tenant_context_rejects_non_utc_auth_time() -> None:
    values = valid_context_values()
    values["auth_time"] = datetime(2026, 8, 5, tzinfo=timezone(timedelta(hours=8)))

    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        TenantContext.model_validate(values)


def test_tenant_context_rejects_extra_fields() -> None:
    values = valid_context_values()
    values["role"] = "tenant_admin"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TenantContext.model_validate(values)

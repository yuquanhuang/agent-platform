"""Internal Artifact retention administration tests."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from packages.application.artifacts import (
    ArtifactLegalHoldRecord,
    ArtifactRetentionAdministrationService,
)
from packages.contracts.public import SubjectType, TenantContext

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
ARTIFACT_ID = UUID("33333333-3333-4333-8333-333333333333")
HOLD_ID = UUID("44444444-4444-4444-8444-444444444444")
NOW = datetime.now(UTC)


class RetentionAdministrationStub:
    def __init__(self) -> None:
        self.case_ref: str | None = None
        self.reason: str | None = None

    async def place_legal_hold(self, context: TenantContext, **kwargs: object):
        self.case_ref = str(kwargs["case_ref"])
        self.reason = str(kwargs["reason"])
        return _hold()

    async def release_legal_hold(self, context: TenantContext, **kwargs: object):
        self.case_ref = str(kwargs["case_ref"])
        self.reason = str(kwargs["reason"])
        return _hold()

    async def retry_failed_delete(self, context: TenantContext, **kwargs: object):
        self.reason = str(kwargs["reason"])
        return HOLD_ID


def _context() -> TenantContext:
    return TenantContext(
        tenant_id=str(TENANT_ID),
        subject_type=SubjectType.SERVICE,
        subject_id=str(ACTOR_ID),
        auth_time=NOW,
        request_id="req-retention",
        trace_id="trace-retention",
    )


def _hold() -> ArtifactLegalHoldRecord:
    return ArtifactLegalHoldRecord(
        id=HOLD_ID,
        tenant_id=TENANT_ID,
        artifact_id=ARTIFACT_ID,
        case_ref="CASE-42",
        reason="preserve evidence",
        placed_by=ACTOR_ID,
        placed_at=NOW,
        released_by=None,
        released_at=None,
    )


@pytest.mark.asyncio
async def test_internal_hold_service_validates_and_normalizes_audit_text() -> None:
    stub = RetentionAdministrationStub()
    service = ArtifactRetentionAdministrationService(stub)

    hold = await service.place_hold(
        _context(),
        artifact_id=ARTIFACT_ID,
        case_ref="  CASE-42  ",
        reason="  preserve evidence  ",
        now=NOW,
    )

    assert hold.id == HOLD_ID
    assert stub.case_ref == "CASE-42"
    assert stub.reason == "preserve evidence"


@pytest.mark.asyncio
async def test_internal_hold_service_rejects_empty_case_reference() -> None:
    service = ArtifactRetentionAdministrationService(RetentionAdministrationStub())

    with pytest.raises(ValueError, match="case_ref"):
        await service.place_hold(
            _context(),
            artifact_id=ARTIFACT_ID,
            case_ref=" ",
            reason="preserve evidence",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_internal_retry_service_requires_auditable_reason() -> None:
    service = ArtifactRetentionAdministrationService(RetentionAdministrationStub())

    with pytest.raises(ValueError, match="retry reason"):
        await service.retry_delete(
            _context(), artifact_id=ARTIFACT_ID, reason=" ", now=NOW
        )

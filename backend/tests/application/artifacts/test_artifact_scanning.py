"""Artifact scan processor and bounded Outbox dispatch tests."""

from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID

import pytest

from packages.application.artifacts import (
    ARTIFACT_SCAN_REQUESTED_EVENT,
    ArtifactScanDispatcher,
    ArtifactScanProcessor,
    ArtifactScanVerdict,
    RetryableArtifactScanError,
)
from packages.contracts.public import SubjectType, TenantContext
from packages.domain.public import (
    ArtifactRecord,
    ArtifactStatus,
    OutboxEvent,
    OutboxStatus,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
ARTIFACT_ID = UUID("33333333-3333-4333-8333-333333333333")
EVENT_ID = UUID("44444444-4444-4444-8444-444444444444")
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
HASH = "sha256:" + "a" * 64


class ScanStub:
    def __init__(self, *, decision: str = "PASSED") -> None:
        self.record = _record()
        self.decision = decision
        self.promoted = 0
        self.completed: list[tuple[str, str | None]] = []
        self.failed: list[str] = []

    async def get_for_scan(self, context: TenantContext, *, artifact_id: UUID):
        return self.record

    async def complete_scan(self, context: TenantContext, **kwargs: object):
        status = cast(str, kwargs["status"])
        object_uri = cast(str | None, kwargs["object_uri"])
        self.completed.append((status, object_uri))
        self.record = _record(status=status)
        return self.record

    async def fail_scan(self, context: TenantContext, **kwargs: object) -> None:
        self.failed.append(cast(str, kwargs["code"]))

    async def scan(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> ArtifactScanVerdict:
        if self.decision == "RETRY":
            raise RetryableArtifactScanError("scanner unavailable")
        return ArtifactScanVerdict(
            decision=cast(Literal["PASSED", "REJECTED"], self.decision),
            engine="test-scanner",
            definition_version="2026-08-09",
            findings=("malware-signature",) if self.decision == "REJECTED" else (),
            scanned_at=NOW,
        )

    async def promote(self, context: TenantContext, *, artifact: ArtifactRecord) -> str:
        self.promoted += 1
        return f"artifact://tenant/{TENANT_ID}/artifact/{ARTIFACT_ID}"


class OutboxStub:
    def __init__(self, event: OutboxEvent) -> None:
        self.event = event
        self.published: list[UUID] = []
        self.retried: list[UUID] = []
        self.dead: list[UUID] = []

    async def claim_ready(self, context: TenantContext, **kwargs: object):
        return (self.event,)

    async def mark_published(
        self, context: TenantContext, event_id: UUID, *, now: datetime
    ) -> None:
        self.published.append(event_id)

    async def mark_retry(
        self,
        context: TenantContext,
        event_id: UUID,
        *,
        next_attempt_at: datetime,
    ) -> None:
        self.retried.append(event_id)

    async def mark_dead(
        self, context: TenantContext, event_id: UUID, *, now: datetime
    ) -> None:
        self.dead.append(event_id)


def _record(*, status: str = "SCANNING") -> ArtifactRecord:
    return ArtifactRecord(
        id=ARTIFACT_ID,
        tenant_id=TENANT_ID,
        workspace_id=None,
        run_id=None,
        owner_user_id=ACTOR_ID,
        name="result.txt",
        quarantine_object_uri=(
            f"quarantine://tenant/{TENANT_ID}/artifact/{ARTIFACT_ID}/source"
        ),
        object_uri=None,
        content_hash=HASH,
        size_bytes=12,
        content_type="text/plain",
        status=cast(ArtifactStatus, status),
        required_output=False,
        scan_result=None,
        upload_expires_at=NOW - timedelta(minutes=1),
        created_at=NOW - timedelta(minutes=10),
        updated_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(days=30),
        retention_delete_after=None,
        deleted_at=None,
    )


def _event(*, attempts: int = 1, valid: bool = True) -> OutboxEvent:
    return OutboxEvent(
        id=EVENT_ID,
        tenant_id=TENANT_ID,
        aggregate_type="artifact",
        aggregate_id=ARTIFACT_ID,
        event_type=ARTIFACT_SCAN_REQUESTED_EVENT,
        payload={"artifact_id": str(ARTIFACT_ID)} if valid else {},
        payload_schema_version=1,
        status=OutboxStatus.PUBLISHING,
        attempts=attempts,
        next_attempt_at=NOW,
        created_at=NOW,
    )


def _context() -> TenantContext:
    return TenantContext(
        tenant_id=str(TENANT_ID),
        subject_type=SubjectType.SERVICE,
        subject_id=str(ACTOR_ID),
        auth_time=NOW,
        request_id="req-artifact-scan",
        trace_id="trace-artifact-scan",
    )


@pytest.mark.asyncio
async def test_scan_pass_promotes_before_available() -> None:
    stub = ScanStub()
    processor = ArtifactScanProcessor(stub, stub, stub)

    await processor.process(_context(), _event())

    assert stub.promoted == 1
    assert stub.completed == [
        (
            "AVAILABLE",
            f"artifact://tenant/{TENANT_ID}/artifact/{ARTIFACT_ID}",
        )
    ]


@pytest.mark.asyncio
async def test_scan_rejection_never_promotes_trusted_object() -> None:
    stub = ScanStub(decision="REJECTED")
    processor = ArtifactScanProcessor(stub, stub, stub)

    await processor.process(_context(), _event())

    assert stub.promoted == 0
    assert stub.completed == [("REJECTED", None)]


@pytest.mark.asyncio
async def test_dispatcher_retries_dependency_failure_with_bound() -> None:
    scan = ScanStub(decision="RETRY")
    outbox = OutboxStub(_event(attempts=2))
    dispatcher = ArtifactScanDispatcher(
        outbox,
        ArtifactScanProcessor(scan, scan, scan),
        max_attempts=3,
    )

    summary = await dispatcher.dispatch_tenant_once(_context(), now=NOW)

    assert summary.retried == 1
    assert outbox.retried == [EVENT_ID]
    assert scan.failed == []


@pytest.mark.asyncio
async def test_dispatcher_exhaustion_marks_artifact_failed_and_event_dead() -> None:
    scan = ScanStub(decision="RETRY")
    outbox = OutboxStub(_event(attempts=3))
    dispatcher = ArtifactScanDispatcher(
        outbox,
        ArtifactScanProcessor(scan, scan, scan),
        max_attempts=3,
    )

    summary = await dispatcher.dispatch_tenant_once(_context(), now=NOW)

    assert summary.dead == 1
    assert scan.failed == ["ARTIFACT_SCAN_RETRIES_EXHAUSTED"]
    assert outbox.dead == [EVENT_ID]


@pytest.mark.asyncio
async def test_invalid_event_is_dead_without_attempting_artifact_failure() -> None:
    scan = ScanStub()
    outbox = OutboxStub(_event(valid=False))
    dispatcher = ArtifactScanDispatcher(
        outbox,
        ArtifactScanProcessor(scan, scan, scan),
    )

    summary = await dispatcher.dispatch_tenant_once(_context(), now=NOW)

    assert summary.dead == 1
    assert scan.failed == []
    assert outbox.dead == [EVENT_ID]

"""Artifact expiry and asynchronous object cleanup tests."""

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest

from packages.application.artifacts import (
    ARTIFACT_DELETE_REQUESTED_EVENT,
    ArtifactDeleteProcessor,
    ArtifactLifecycleDispatcher,
    RetryableArtifactDeleteError,
)
from packages.application.outbox import OutboxStore
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
OPERATION_ID = UUID("44444444-4444-4444-8444-444444444444")
EVENT_ID = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime.now(UTC)


class LifecycleStub:
    def __init__(self) -> None:
        self.artifact = _artifact()
        self.events = [_event()]
        self.reclaimed = 0
        self.expired = 0
        self.purged = 0
        self.recovered = 0
        self.deleted = 0
        self.failed_codes: list[str] = []
        self.published: list[UUID] = []
        self.retried: list[UUID] = []
        self.dead: list[UUID] = []
        self.object_error: Exception | None = None
        self.object_actions: list[str] = []

    async def expire_due(self, context: TenantContext, **kwargs: object) -> int:
        self.expired += 1
        return 0

    async def reclaim_expired_uploads(
        self, context: TenantContext, **kwargs: object
    ) -> int:
        self.reclaimed += 1
        return 0

    async def purge_retention_due(
        self, context: TenantContext, **kwargs: object
    ) -> int:
        self.purged += 1
        return 0

    async def recover_failed_deletes(
        self, context: TenantContext, **kwargs: object
    ) -> int:
        self.recovered += 1
        return 0

    async def get_for_delete(self, context: TenantContext, **kwargs: object):
        return self.artifact

    async def complete_delete(self, context: TenantContext, **kwargs: object) -> None:
        self.deleted += 1
        self.artifact = _artifact(status="DELETED")

    async def fail_delete(self, context: TenantContext, **kwargs: object) -> None:
        self.failed_codes.append(cast(str, kwargs["code"]))

    async def revoke_download_access(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> None:
        self.object_actions.append("revoke")
        if self.object_error is not None:
            raise self.object_error

    async def delete_artifact_objects(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> None:
        self.object_actions.append("delete")
        if self.object_error is not None:
            raise self.object_error

    async def claim_ready(self, context: TenantContext, **kwargs: object):
        return tuple(self.events)

    async def mark_published(
        self, context: TenantContext, event_id: UUID, **kwargs: object
    ) -> None:
        self.published.append(event_id)

    async def mark_retry(
        self, context: TenantContext, event_id: UUID, **kwargs: object
    ) -> None:
        self.retried.append(event_id)

    async def mark_dead(
        self, context: TenantContext, event_id: UUID, **kwargs: object
    ) -> None:
        self.dead.append(event_id)


def _artifact(*, status: str = "DELETING") -> ArtifactRecord:
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
        object_uri=f"artifact://tenant/{TENANT_ID}/artifact/{ARTIFACT_ID}",
        content_hash="sha256:" + "a" * 64,
        size_bytes=12,
        content_type="text/plain",
        status=cast(ArtifactStatus, status),
        required_output=False,
        scan_result={"decision": "PASSED"},
        upload_expires_at=NOW + timedelta(minutes=15),
        created_at=NOW,
        updated_at=NOW,
        expires_at=NOW + timedelta(days=30),
        retention_delete_after=None,
        deleted_at=NOW if status == "DELETED" else None,
    )


def _event(*, attempts: int = 1, valid: bool = True) -> OutboxEvent:
    return OutboxEvent(
        id=EVENT_ID,
        tenant_id=TENANT_ID,
        aggregate_type="artifact",
        aggregate_id=ARTIFACT_ID,
        event_type=ARTIFACT_DELETE_REQUESTED_EVENT,
        payload=(
            {
                "artifact_id": str(ARTIFACT_ID),
                "operation_id": str(OPERATION_ID),
            }
            if valid
            else {}
        ),
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
        membership_version=1,
        auth_time=NOW,
        request_id="req-artifact-delete",
        trace_id="trace-artifact-delete",
    )


def _dispatcher(stub: LifecycleStub, *, max_attempts: int = 3):
    return ArtifactLifecycleDispatcher(
        stub,
        cast(OutboxStore, stub),
        ArtifactDeleteProcessor(stub, stub),
        max_attempts=max_attempts,
    )


@pytest.mark.asyncio
async def test_lifecycle_expires_due_then_deletes_objects_and_tombstones() -> None:
    stub = LifecycleStub()

    summary = await _dispatcher(stub).dispatch_tenant_once(_context(), now=NOW)

    assert stub.reclaimed == 1
    assert stub.expired == 1
    assert stub.purged == 1
    assert stub.recovered == 1
    assert stub.deleted == 1
    assert stub.object_actions == ["revoke", "delete"]
    assert stub.published == [EVENT_ID]
    assert summary.published == 1


@pytest.mark.asyncio
async def test_retryable_cleanup_failure_keeps_deleting_for_retry() -> None:
    stub = LifecycleStub()
    stub.object_error = RetryableArtifactDeleteError("object store unavailable")

    summary = await _dispatcher(stub).dispatch_tenant_once(_context(), now=NOW)

    assert stub.deleted == 0
    assert stub.retried == [EVENT_ID]
    assert stub.failed_codes == []
    assert summary.retried == 1


@pytest.mark.asyncio
async def test_cleanup_retry_exhaustion_fails_operation_and_dead_letters() -> None:
    stub = LifecycleStub()
    stub.events = [_event(attempts=3)]
    stub.object_error = RetryableArtifactDeleteError("object store unavailable")

    summary = await _dispatcher(stub, max_attempts=3).dispatch_tenant_once(
        _context(), now=NOW
    )

    assert stub.failed_codes == ["ARTIFACT_DELETE_RETRIES_EXHAUSTED"]
    assert stub.dead == [EVENT_ID]
    assert summary.dead == 1


@pytest.mark.asyncio
async def test_invalid_delete_event_is_dead_without_mutating_unknown_operation() -> (
    None
):
    stub = LifecycleStub()
    stub.events = [_event(valid=False)]

    summary = await _dispatcher(stub).dispatch_tenant_once(_context(), now=NOW)

    assert stub.deleted == 0
    assert stub.failed_codes == []
    assert stub.dead == [EVENT_ID]
    assert summary.dead == 1

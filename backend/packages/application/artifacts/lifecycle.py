"""Artifact retention expiry and retryable physical object deletion."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from packages.application.artifacts.service import ARTIFACT_DELETE_REQUESTED_EVENT
from packages.application.outbox import OutboxDispatchSummary, OutboxStore
from packages.contracts.public import TenantContext
from packages.domain.public import ArtifactRecord, OutboxEvent, retry_delay


class RetryableArtifactDeleteError(RuntimeError):
    """Artifact object cleanup can be retried safely."""


class PermanentArtifactDeleteError(ValueError):
    """The delete event or cleanup request cannot be repaired by retrying."""


class ArtifactDeletionObjectStore(Protocol):
    async def revoke_download_access(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> None:
        """Idempotently revoke previously issued download capabilities."""

    async def delete_artifact_objects(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> None:
        """Idempotently remove quarantine and trusted objects for one Artifact."""


class ArtifactLifecycleStore(Protocol):
    async def reclaim_expired_uploads(
        self, context: TenantContext, *, now: datetime, limit: int
    ) -> int: ...

    async def expire_due(
        self, context: TenantContext, *, now: datetime, limit: int
    ) -> int: ...

    async def purge_retention_due(
        self, context: TenantContext, *, now: datetime, limit: int
    ) -> int: ...

    async def recover_failed_deletes(
        self, context: TenantContext, *, now: datetime, limit: int
    ) -> int: ...

    async def get_for_delete(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        operation_id: UUID,
    ) -> ArtifactRecord | None: ...

    async def complete_delete(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        operation_id: UUID,
        now: datetime,
    ) -> None: ...

    async def fail_delete(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        operation_id: UUID,
        code: str,
        now: datetime,
    ) -> None: ...


class ArtifactDeleteProcessor:
    def __init__(
        self,
        store: ArtifactLifecycleStore,
        objects: ArtifactDeletionObjectStore,
        *,
        timeout: timedelta = timedelta(seconds=30),
    ) -> None:
        if timeout <= timedelta(0):
            raise ValueError("Artifact delete timeout must be positive")
        self._store = store
        self._objects = objects
        self._timeout = timeout

    async def process(
        self, context: TenantContext, event: OutboxEvent, *, now: datetime
    ) -> None:
        artifact_id, operation_id = _delete_event_ids(event)
        artifact = await self._store.get_for_delete(
            context,
            artifact_id=artifact_id,
            operation_id=operation_id,
        )
        if artifact is None:
            raise PermanentArtifactDeleteError("Artifact delete target does not exist")
        if artifact.status == "DELETED":
            await self._store.complete_delete(
                context,
                artifact_id=artifact_id,
                operation_id=operation_id,
                now=now,
            )
            return
        if artifact.status != "DELETING":
            raise PermanentArtifactDeleteError("Artifact is not awaiting deletion")
        try:
            await asyncio.wait_for(
                self._objects.revoke_download_access(context, artifact=artifact),
                timeout=self._timeout.total_seconds(),
            )
            await asyncio.wait_for(
                self._objects.delete_artifact_objects(context, artifact=artifact),
                timeout=self._timeout.total_seconds(),
            )
        except asyncio.CancelledError:
            raise
        except PermanentArtifactDeleteError:
            raise
        except (TimeoutError, RetryableArtifactDeleteError) as error:
            raise RetryableArtifactDeleteError(
                "Artifact object cleanup dependency failed"
            ) from error
        await self._store.complete_delete(
            context,
            artifact_id=artifact_id,
            operation_id=operation_id,
            now=now,
        )

    async def fail(
        self, context: TenantContext, event: OutboxEvent, *, code: str, now: datetime
    ) -> None:
        identifiers = _delete_event_ids_or_none(event)
        if identifiers is None:
            return
        artifact_id, operation_id = identifiers
        await self._store.fail_delete(
            context,
            artifact_id=artifact_id,
            operation_id=operation_id,
            code=code,
            now=now,
        )


class ArtifactLifecycleDispatcher:
    """Reclaim abandoned uploads, expire retention, then process deletions."""

    def __init__(
        self,
        lifecycle_store: ArtifactLifecycleStore,
        outbox_store: OutboxStore,
        processor: ArtifactDeleteProcessor,
        *,
        batch_size: int = 20,
        max_attempts: int = 10,
        lease_duration: timedelta = timedelta(seconds=60),
    ) -> None:
        if batch_size < 1 or max_attempts < 1 or lease_duration <= timedelta(0):
            raise ValueError("Artifact lifecycle dispatcher limits must be positive")
        self._lifecycle_store = lifecycle_store
        self._outbox_store = outbox_store
        self._processor = processor
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._lease_duration = lease_duration

    async def dispatch_tenant_once(
        self, context: TenantContext, *, now: datetime
    ) -> OutboxDispatchSummary:
        await self._lifecycle_store.reclaim_expired_uploads(
            context, now=now, limit=self._batch_size
        )
        await self._lifecycle_store.expire_due(context, now=now, limit=self._batch_size)
        await self._lifecycle_store.purge_retention_due(
            context, now=now, limit=self._batch_size
        )
        await self._lifecycle_store.recover_failed_deletes(
            context, now=now, limit=self._batch_size
        )
        events: Sequence[OutboxEvent] = await self._outbox_store.claim_ready(
            context,
            now=now,
            limit=self._batch_size,
            lease_duration=self._lease_duration,
        )
        published = retried = dead = 0
        for event in events:
            try:
                await self._processor.process(context, event, now=now)
            except asyncio.CancelledError:
                raise
            except PermanentArtifactDeleteError:
                await self._processor.fail(
                    context,
                    event,
                    code="ARTIFACT_DELETE_PERMANENT_FAILURE",
                    now=now,
                )
                await self._outbox_store.mark_dead(context, event.id, now=now)
                dead += 1
            except RetryableArtifactDeleteError:
                if event.attempts >= self._max_attempts:
                    await self._processor.fail(
                        context,
                        event,
                        code="ARTIFACT_DELETE_RETRIES_EXHAUSTED",
                        now=now,
                    )
                    await self._outbox_store.mark_dead(context, event.id, now=now)
                    dead += 1
                else:
                    await self._outbox_store.mark_retry(
                        context,
                        event.id,
                        next_attempt_at=now + retry_delay(event.attempts),
                    )
                    retried += 1
            else:
                await self._outbox_store.mark_published(context, event.id, now=now)
                published += 1
        return OutboxDispatchSummary(
            claimed=len(events),
            published=published,
            retried=retried,
            dead=dead,
        )


def _delete_event_ids(event: OutboxEvent) -> tuple[UUID, UUID]:
    if event.event_type != ARTIFACT_DELETE_REQUESTED_EVENT:
        raise PermanentArtifactDeleteError(
            "Outbox event type is not an Artifact deletion"
        )
    raw_artifact_id = event.payload.get("artifact_id")
    raw_operation_id = event.payload.get("operation_id")
    if not isinstance(raw_artifact_id, str) or not isinstance(raw_operation_id, str):
        raise PermanentArtifactDeleteError("Artifact delete payload is invalid")
    try:
        artifact_id = UUID(raw_artifact_id)
        operation_id = UUID(raw_operation_id)
    except ValueError as error:
        raise PermanentArtifactDeleteError(
            "Artifact delete identifiers are invalid"
        ) from error
    if artifact_id != event.aggregate_id:
        raise PermanentArtifactDeleteError("Artifact delete aggregate identity drifted")
    return artifact_id, operation_id


def _delete_event_ids_or_none(event: OutboxEvent) -> tuple[UUID, UUID] | None:
    try:
        return _delete_event_ids(event)
    except PermanentArtifactDeleteError:
        return None

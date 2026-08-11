"""Durable Artifact scan processing outside API database transactions."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID

from pydantic import JsonValue

from packages.application.artifacts.service import ARTIFACT_SCAN_REQUESTED_EVENT
from packages.application.outbox import OutboxDispatchSummary, OutboxStore
from packages.contracts.public import TenantContext
from packages.domain.public import ArtifactRecord, OutboxEvent, retry_delay


@dataclass(frozen=True, slots=True)
class ArtifactScanVerdict:
    decision: Literal["PASSED", "REJECTED"]
    engine: str
    definition_version: str
    findings: tuple[str, ...]
    scanned_at: datetime

    def __post_init__(self) -> None:
        if not self.engine or len(self.engine) > 128:
            raise ValueError("Artifact scan engine is invalid")
        if not self.definition_version or len(self.definition_version) > 128:
            raise ValueError("Artifact scan definition version is invalid")
        if len(self.findings) > 100 or any(
            not finding or len(finding) > 128 for finding in self.findings
        ):
            raise ValueError("Artifact scan findings are invalid")
        offset = self.scanned_at.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError("Artifact scan time must be timezone-aware UTC")


class RetryableArtifactScanError(RuntimeError):
    """Artifact scanning or promotion can be retried safely."""


class PermanentArtifactScanError(ValueError):
    """The event or Artifact cannot be repaired by retrying."""


class ArtifactSecurityScanner(Protocol):
    async def scan(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> ArtifactScanVerdict: ...


class ArtifactTrustedObjectPublisher(Protocol):
    async def promote(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> str: ...


class ArtifactScanStore(Protocol):
    async def get_for_scan(
        self, context: TenantContext, *, artifact_id: UUID
    ) -> ArtifactRecord | None: ...

    async def complete_scan(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        status: Literal["AVAILABLE", "REJECTED"],
        object_uri: str | None,
        scan_result: dict[str, JsonValue],
        now: datetime,
    ) -> ArtifactRecord: ...

    async def fail_scan(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        code: str,
        now: datetime,
    ) -> None: ...


class ArtifactScanProcessor:
    def __init__(
        self,
        store: ArtifactScanStore,
        scanner: ArtifactSecurityScanner,
        publisher: ArtifactTrustedObjectPublisher,
        *,
        timeout: timedelta = timedelta(seconds=60),
    ) -> None:
        if timeout <= timedelta(0):
            raise ValueError("Artifact scan timeout must be positive")
        self._store = store
        self._scanner = scanner
        self._publisher = publisher
        self._timeout = timeout

    async def process(self, context: TenantContext, event: OutboxEvent) -> None:
        artifact_id = _artifact_event_id(event)
        record = await self._store.get_for_scan(context, artifact_id=artifact_id)
        if record is None:
            raise PermanentArtifactScanError("Artifact scan target does not exist")
        if record.status in {"AVAILABLE", "REJECTED", "FAILED"}:
            return
        if record.status != "SCANNING":
            raise PermanentArtifactScanError("Artifact is not awaiting a scan")
        try:
            verdict = await asyncio.wait_for(
                self._scanner.scan(context, artifact=record),
                timeout=self._timeout.total_seconds(),
            )
            object_uri: str | None = None
            status: Literal["AVAILABLE", "REJECTED"] = "REJECTED"
            if verdict.decision == "PASSED":
                object_uri = await asyncio.wait_for(
                    self._publisher.promote(context, artifact=record),
                    timeout=self._timeout.total_seconds(),
                )
                if not object_uri or any(
                    ord(character) < 32 for character in object_uri
                ):
                    raise PermanentArtifactScanError(
                        "Artifact publisher returned an invalid trusted object URI"
                    )
                status = "AVAILABLE"
            await self._store.complete_scan(
                context,
                artifact_id=record.id,
                status=status,
                object_uri=object_uri,
                scan_result=_scan_result(verdict),
                now=verdict.scanned_at,
            )
        except asyncio.CancelledError:
            raise
        except PermanentArtifactScanError:
            raise
        except (TimeoutError, RetryableArtifactScanError) as error:
            raise RetryableArtifactScanError(
                "Artifact scan dependency failed"
            ) from error

    async def fail(self, context: TenantContext, event: OutboxEvent, code: str) -> None:
        artifact_id = _artifact_event_id_or_none(event)
        if artifact_id is None:
            return
        await self._store.fail_scan(
            context,
            artifact_id=artifact_id,
            code=code,
            now=datetime.now(UTC),
        )


class ArtifactScanDispatcher:
    """Claim only Artifact scan events and converge retries to FAILED."""

    def __init__(
        self,
        store: OutboxStore,
        processor: ArtifactScanProcessor,
        *,
        batch_size: int = 20,
        max_attempts: int = 10,
        lease_duration: timedelta = timedelta(seconds=90),
    ) -> None:
        if batch_size < 1 or max_attempts < 1 or lease_duration <= timedelta(0):
            raise ValueError("Artifact dispatcher limits must be positive")
        self._store = store
        self._processor = processor
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._lease_duration = lease_duration

    async def dispatch_tenant_once(
        self, context: TenantContext, *, now: datetime
    ) -> OutboxDispatchSummary:
        events: Sequence[OutboxEvent] = await self._store.claim_ready(
            context,
            now=now,
            limit=self._batch_size,
            lease_duration=self._lease_duration,
        )
        published = retried = dead = 0
        for event in events:
            try:
                await self._processor.process(context, event)
            except asyncio.CancelledError:
                raise
            except PermanentArtifactScanError:
                await self._processor.fail(
                    context, event, "ARTIFACT_SCAN_PERMANENT_FAILURE"
                )
                await self._store.mark_dead(context, event.id, now=now)
                dead += 1
            except RetryableArtifactScanError:
                if event.attempts >= self._max_attempts:
                    await self._processor.fail(
                        context, event, "ARTIFACT_SCAN_RETRIES_EXHAUSTED"
                    )
                    await self._store.mark_dead(context, event.id, now=now)
                    dead += 1
                else:
                    await self._store.mark_retry(
                        context,
                        event.id,
                        next_attempt_at=now + retry_delay(event.attempts),
                    )
                    retried += 1
            else:
                await self._store.mark_published(context, event.id, now=now)
                published += 1
        return OutboxDispatchSummary(
            claimed=len(events),
            published=published,
            retried=retried,
            dead=dead,
        )


def _artifact_event_id(event: OutboxEvent) -> UUID:
    if event.event_type != ARTIFACT_SCAN_REQUESTED_EVENT:
        raise PermanentArtifactScanError("Outbox event type is not an Artifact scan")
    raw_id = event.payload.get("artifact_id")
    if not isinstance(raw_id, str):
        raise PermanentArtifactScanError("Artifact scan payload is invalid")
    try:
        artifact_id = UUID(raw_id)
    except ValueError as error:
        raise PermanentArtifactScanError(
            "Artifact scan identifier is invalid"
        ) from error
    if artifact_id != event.aggregate_id:
        raise PermanentArtifactScanError("Artifact scan aggregate identity drifted")
    return artifact_id


def _artifact_event_id_or_none(event: OutboxEvent) -> UUID | None:
    try:
        return _artifact_event_id(event)
    except PermanentArtifactScanError:
        return None


def _scan_result(verdict: ArtifactScanVerdict) -> dict[str, JsonValue]:
    return {
        "schema_version": "artifact-scan/v1",
        "decision": verdict.decision,
        "engine": verdict.engine,
        "definition_version": verdict.definition_version,
        "findings": list(verdict.findings),
        "scanned_at": verdict.scanned_at.isoformat(),
    }

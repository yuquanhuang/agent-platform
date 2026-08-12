"""Revocable Artifact Download Gateway application boundary."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from pydantic import SecretStr

from packages.application.metadata import RequestMetadata
from packages.contracts.public import (
    SubjectType,
    TenantContext,
    dependency_unavailable,
    range_not_satisfiable,
    rate_limited,
    resource_not_found,
)
from packages.domain.public import ArtifactRecord, artifact_uri

_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
LOGGER = logging.getLogger(__name__)
ARTIFACT_DOWNLOADS_REVOKED_EVENT = "artifact.downloads_revoked.v1"


@dataclass(frozen=True, slots=True)
class ArtifactDownloadCredential:
    """One bearer capability whose plaintext is returned only to its caller."""

    grant_id: UUID
    token: SecretStr
    token_hash: str


class ArtifactDownloadCredentialIssuer(Protocol):
    def issue(self) -> ArtifactDownloadCredential: ...


@dataclass(frozen=True, slots=True)
class ArtifactDownloadGrantRecord:
    id: UUID
    tenant_id: UUID
    artifact_id: UUID
    owner_user_id: UUID
    token_hash: str
    expires_at: datetime
    revoked_at: datetime | None
    artifact: ArtifactRecord


class ArtifactDownloadGrantStore(Protocol):
    async def resolve_download_grant(
        self,
        *,
        grant_id: UUID,
        token_hash: str,
        service_subject_id: UUID,
        metadata: RequestMetadata,
        now: datetime,
    ) -> ArtifactDownloadGrantRecord | None: ...

    async def record_download_open(
        self,
        context: TenantContext,
        *,
        grant_id: UUID,
        artifact_id: UUID,
        metadata: RequestMetadata,
        now: datetime,
    ) -> bool: ...

    async def is_download_grant_active(
        self,
        context: TenantContext,
        *,
        grant_id: UUID,
        artifact_id: UUID,
        now: datetime,
    ) -> bool: ...


class ArtifactDownloadRevocationSubscription(Protocol):
    async def wait(self, *, timeout_seconds: float) -> bool: ...

    async def aclose(self) -> None: ...


class ArtifactDownloadRevocationSource(Protocol):
    async def subscribe(
        self,
        *,
        tenant_id: UUID,
        artifact_id: UUID,
    ) -> ArtifactDownloadRevocationSubscription: ...


class ArtifactDownloadRevocationUnavailable(RuntimeError):
    """The Redis wake-up path is unavailable; PostgreSQL polling remains valid."""


@dataclass(frozen=True, slots=True)
class ArtifactTrustedContent:
    body: AsyncIterator[bytes]
    size_bytes: int
    total_size_bytes: int
    content_type: str
    name: str
    byte_range: ArtifactByteRange | None = None


@dataclass(frozen=True, slots=True)
class ArtifactByteRange:
    start: int
    end_inclusive: int

    @property
    def length(self) -> int:
        return self.end_inclusive - self.start + 1


class ArtifactDownloadObjectNotFound(RuntimeError):
    """The trusted object is missing despite an active durable grant."""


class ArtifactDownloadObjectUnavailable(RuntimeError):
    """The private object store cannot open the trusted object."""


class ArtifactDownloadCapacityExceeded(RuntimeError):
    """The deployment download concurrency budget is currently exhausted."""


class ArtifactDownloadObjectReader(Protocol):
    async def open_trusted_artifact(
        self,
        context: TenantContext,
        *,
        artifact: ArtifactRecord,
        byte_range: ArtifactByteRange | None,
    ) -> ArtifactTrustedContent: ...


class ArtifactDownloadGatewayService:
    """Resolve a bearer grant, then stream only one canonical private object."""

    def __init__(
        self,
        store: ArtifactDownloadGrantStore,
        objects: ArtifactDownloadObjectReader,
        *,
        service_subject_id: UUID,
        revocation_source: ArtifactDownloadRevocationSource | None = None,
        revocation_check_interval: timedelta = timedelta(seconds=5),
        max_stream_duration: timedelta = timedelta(hours=1),
    ) -> None:
        if revocation_check_interval <= timedelta(0):
            raise ValueError("Download revocation check interval must be positive")
        if max_stream_duration <= timedelta(0):
            raise ValueError("Download maximum stream duration must be positive")
        self._store = store
        self._objects = objects
        self._service_subject_id = service_subject_id
        self._revocation_source = revocation_source
        self._revocation_check_interval = revocation_check_interval
        self._max_stream_duration = max_stream_duration

    async def open_download(
        self,
        *,
        grant_id: str,
        token: str,
        range_header: str | None,
        metadata: RequestMetadata,
    ) -> ArtifactTrustedContent:
        resource_id = _grant_id(grant_id)
        token_hash = artifact_download_token_hash(token)
        now = datetime.now(UTC)
        grant = await self._store.resolve_download_grant(
            grant_id=resource_id,
            token_hash=token_hash,
            service_subject_id=self._service_subject_id,
            metadata=metadata,
            now=now,
        )
        if grant is None:
            raise resource_not_found()
        artifact = grant.artifact
        if artifact.object_uri != artifact_uri(
            tenant_id=grant.tenant_id, artifact_id=grant.artifact_id
        ):
            raise dependency_unavailable(
                "Artifact trusted storage mapping is unavailable."
            )
        context = TenantContext(
            tenant_id=str(grant.tenant_id),
            subject_type=SubjectType.SERVICE,
            subject_id=str(self._service_subject_id),
            auth_time=now,
            request_id=metadata.request_id,
            trace_id=metadata.trace_id,
        )
        byte_range = resolve_artifact_byte_range(
            range_header,
            total_size=artifact.size_bytes,
        )
        try:
            content = await self._objects.open_trusted_artifact(
                context,
                artifact=artifact,
                byte_range=byte_range,
            )
        except asyncio.CancelledError:
            raise
        except ArtifactDownloadObjectNotFound as error:
            raise dependency_unavailable(
                "Artifact trusted object is unavailable."
            ) from error
        except ArtifactDownloadObjectUnavailable as error:
            raise dependency_unavailable(
                "Artifact object storage is unavailable."
            ) from error
        except ArtifactDownloadCapacityExceeded as error:
            raise rate_limited(
                "Artifact download capacity is temporarily exhausted."
            ) from error
        _validate_content(content, artifact)
        opened = await self._store.record_download_open(
            context,
            grant_id=grant.id,
            artifact_id=grant.artifact_id,
            metadata=metadata,
            now=datetime.now(UTC),
        )
        if not opened:
            await _close(content.body)
            raise resource_not_found()
        return ArtifactTrustedContent(
            body=_RevocableArtifactBody(
                content.body,
                store=self._store,
                source=self._revocation_source,
                context=context,
                grant_id=grant.id,
                artifact_id=grant.artifact_id,
                check_interval_seconds=(
                    self._revocation_check_interval.total_seconds()
                ),
                max_duration_seconds=self._max_stream_duration.total_seconds(),
            ),
            size_bytes=content.size_bytes,
            total_size_bytes=content.total_size_bytes,
            content_type=content.content_type,
            name=content.name,
            byte_range=content.byte_range,
        )


def artifact_download_token_hash(token: str) -> str:
    if _TOKEN_PATTERN.fullmatch(token) is None:
        raise resource_not_found()
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()


def _grant_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as error:
        raise resource_not_found() from error


def _validate_content(
    content: ArtifactTrustedContent, artifact: ArtifactRecord
) -> None:
    if (
        content.total_size_bytes != artifact.size_bytes
        or content.content_type != artifact.content_type
        or content.name != artifact.name
        or content.size_bytes < 1
    ):
        raise dependency_unavailable("Artifact object metadata is inconsistent.")
    if content.byte_range is None:
        if content.size_bytes != artifact.size_bytes:
            raise dependency_unavailable("Artifact object metadata is inconsistent.")
        return
    if (
        content.byte_range.start < 0
        or content.byte_range.end_inclusive >= artifact.size_bytes
        or content.byte_range.end_inclusive < content.byte_range.start
        or content.size_bytes != content.byte_range.length
    ):
        raise dependency_unavailable("Artifact object range metadata is inconsistent.")


def resolve_artifact_byte_range(
    value: str | None,
    *,
    total_size: int,
) -> ArtifactByteRange | None:
    """Resolve one RFC 9110 byte range against immutable Artifact size."""

    if total_size < 1:
        raise range_not_satisfiable(total_size=max(total_size, 0))
    if value is None:
        return None
    if len(value) > 128 or not value.startswith("bytes=") or "," in value:
        raise range_not_satisfiable(total_size=total_size)
    spec = value[6:]
    if spec.count("-") != 1:
        raise range_not_satisfiable(total_size=total_size)
    start_value, end_value = spec.split("-", maxsplit=1)
    if start_value:
        if not start_value.isascii() or not start_value.isdecimal():
            raise range_not_satisfiable(total_size=total_size)
        start = int(start_value)
        if start >= total_size:
            raise range_not_satisfiable(total_size=total_size)
        if end_value:
            if not end_value.isascii() or not end_value.isdecimal():
                raise range_not_satisfiable(total_size=total_size)
            requested_end = int(end_value)
            if requested_end < start:
                raise range_not_satisfiable(total_size=total_size)
            end = min(requested_end, total_size - 1)
        else:
            end = total_size - 1
        return ArtifactByteRange(start=start, end_inclusive=end)
    if not end_value or not end_value.isascii() or not end_value.isdecimal():
        raise range_not_satisfiable(total_size=total_size)
    suffix_length = int(end_value)
    if suffix_length < 1:
        raise range_not_satisfiable(total_size=total_size)
    length = min(suffix_length, total_size)
    return ArtifactByteRange(
        start=total_size - length,
        end_inclusive=total_size - 1,
    )


async def _close(body: AsyncIterator[bytes]) -> None:
    close = getattr(body, "aclose", None)
    if close is not None:
        await close()


class _RevocableArtifactBody(AsyncIterator[bytes]):
    """Interrupt an open stream when durable Grant facts become inactive."""

    def __init__(
        self,
        body: AsyncIterator[bytes],
        *,
        store: ArtifactDownloadGrantStore,
        source: ArtifactDownloadRevocationSource | None,
        context: TenantContext,
        grant_id: UUID,
        artifact_id: UUID,
        check_interval_seconds: float,
        max_duration_seconds: float,
    ) -> None:
        self._body = body
        self._store = store
        self._source = source
        self._context = context
        self._grant_id = grant_id
        self._artifact_id = artifact_id
        self._check_interval_seconds = check_interval_seconds
        self._max_duration_seconds = max_duration_seconds
        self._subscription: ArtifactDownloadRevocationSubscription | None = None
        self._started_at: float | None = None
        self._chunk_task: asyncio.Future[bytes] | None = None
        self._closed = False

    def __aiter__(self) -> _RevocableArtifactBody:
        return self

    async def __anext__(self) -> bytes:
        if self._closed:
            raise StopAsyncIteration
        if self._started_at is None:
            self._started_at = asyncio.get_running_loop().time()
            await self._subscribe()
            if not await self._is_active():
                await self.aclose()
                raise StopAsyncIteration

        chunk_task = asyncio.ensure_future(anext(self._body))
        self._chunk_task = chunk_task
        try:
            while True:
                remaining = self._remaining_seconds()
                if remaining <= 0:
                    await self.aclose()
                    raise StopAsyncIteration
                wait_seconds = min(self._check_interval_seconds, remaining)
                wake_task = asyncio.create_task(self._wait_for_wake(wait_seconds))
                done, _ = await asyncio.wait(
                    {chunk_task, wake_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if chunk_task in done:
                    wake_task.cancel()
                    await _await_cancelled(wake_task)
                    try:
                        return chunk_task.result()
                    except StopAsyncIteration:
                        await self.aclose()
                        raise
                await wake_task
                if not await self._is_active():
                    await self.aclose()
                    raise StopAsyncIteration
        except asyncio.CancelledError:
            await self.aclose()
            raise
        finally:
            if self._chunk_task is chunk_task:
                self._chunk_task = None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._chunk_task is not None and not self._chunk_task.done():
            self._chunk_task.cancel()
            await _await_cancelled(self._chunk_task)
        self._chunk_task = None
        if self._subscription is not None:
            try:
                await self._subscription.aclose()
            except ArtifactDownloadRevocationUnavailable:
                LOGGER.warning("Artifact download revocation subscription close failed")
            self._subscription = None
        await _close(self._body)

    async def _subscribe(self) -> None:
        if self._source is None:
            return
        try:
            self._subscription = await self._source.subscribe(
                tenant_id=UUID(self._context.tenant_id),
                artifact_id=self._artifact_id,
            )
        except ArtifactDownloadRevocationUnavailable:
            LOGGER.warning(
                "Artifact download revocation notifications unavailable; "
                "using PostgreSQL polling"
            )

    async def _wait_for_wake(self, timeout_seconds: float) -> None:
        if self._subscription is None:
            await asyncio.sleep(timeout_seconds)
            return
        try:
            await self._subscription.wait(timeout_seconds=timeout_seconds)
        except ArtifactDownloadRevocationUnavailable:
            LOGGER.warning(
                "Artifact download revocation notification failed; "
                "using PostgreSQL polling"
            )
            try:
                await self._subscription.aclose()
            except ArtifactDownloadRevocationUnavailable:
                pass
            self._subscription = None

    async def _is_active(self) -> bool:
        return await self._store.is_download_grant_active(
            self._context,
            grant_id=self._grant_id,
            artifact_id=self._artifact_id,
            now=datetime.now(UTC),
        )

    def _remaining_seconds(self) -> float:
        assert self._started_at is not None
        return self._max_duration_seconds - (
            asyncio.get_running_loop().time() - self._started_at
        )


async def _await_cancelled[T](task: asyncio.Future[T]) -> None:
    try:
        await task
    except (asyncio.CancelledError, StopAsyncIteration):
        return

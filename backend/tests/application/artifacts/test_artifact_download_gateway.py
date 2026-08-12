"""Revocable Artifact Download Gateway tests."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from packages.application.artifacts import (
    ArtifactByteRange,
    ArtifactDownloadGatewayService,
    ArtifactDownloadGrantRecord,
    ArtifactDownloadObjectUnavailable,
    ArtifactDownloadRevocationSubscription,
    ArtifactTrustedContent,
    resolve_artifact_byte_range,
)
from packages.application.metadata import RequestMetadata
from packages.contracts.public import PlatformError, TenantContext
from packages.domain.public import ArtifactRecord

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
OWNER_ID = UUID("22222222-2222-4222-8222-222222222222")
ARTIFACT_ID = UUID("33333333-3333-4333-8333-333333333333")
GRANT_ID = UUID("44444444-4444-4444-8444-444444444444")
SERVICE_ID = UUID("55555555-5555-4555-8555-555555555555")
TOKEN = "t" * 43
TOKEN_HASH = "sha256:" + hashlib.sha256(TOKEN.encode()).hexdigest()
NOW = datetime.now(UTC)


class GatewayStub:
    def __init__(self) -> None:
        self.record = _grant()
        self.open_allowed = True
        self.object_unavailable = False
        self.context: TenantContext | None = None
        self.closed = False
        self.active = True
        self.stream_gate: asyncio.Event | None = None

    async def resolve_download_grant(self, **kwargs: object):
        if (
            kwargs["token_hash"] != self.record.token_hash
            or self.record.revoked_at is not None
        ):
            return None
        return self.record

    async def open_trusted_artifact(
        self,
        context: TenantContext,
        *,
        artifact: ArtifactRecord,
        byte_range: ArtifactByteRange | None,
    ) -> ArtifactTrustedContent:
        self.context = context
        if self.object_unavailable:
            raise ArtifactDownloadObjectUnavailable("private store unavailable")

        payload = b"artifact-body"
        if byte_range is not None:
            payload = payload[byte_range.start : byte_range.end_inclusive + 1]

        async def body():
            try:
                if self.stream_gate is None:
                    yield payload
                else:
                    yield payload[:4]
                    await self.stream_gate.wait()
                    yield payload[4:]
            finally:
                self.closed = True

        return ArtifactTrustedContent(
            body=body(),
            size_bytes=len(payload),
            total_size_bytes=artifact.size_bytes,
            content_type=artifact.content_type,
            name=artifact.name,
            byte_range=byte_range,
        )

    async def record_download_open(
        self, context: TenantContext, **kwargs: object
    ) -> bool:
        self.context = context
        return self.open_allowed

    async def is_download_grant_active(
        self, context: TenantContext, **kwargs: object
    ) -> bool:
        self.context = context
        return self.active


class RevocationSubscriptionStub(ArtifactDownloadRevocationSubscription):
    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.closed = False

    async def wait(self, *, timeout_seconds: float) -> bool:
        try:
            await asyncio.wait_for(self.event.wait(), timeout=timeout_seconds)
        except TimeoutError:
            return False
        self.event.clear()
        return True

    async def aclose(self) -> None:
        self.closed = True


class RevocationSourceStub:
    def __init__(self, subscription: RevocationSubscriptionStub) -> None:
        self.subscription = subscription

    async def subscribe(self, **kwargs: object) -> RevocationSubscriptionStub:
        return self.subscription


def _grant() -> ArtifactDownloadGrantRecord:
    artifact = ArtifactRecord(
        id=ARTIFACT_ID,
        tenant_id=TENANT_ID,
        workspace_id=None,
        run_id=None,
        owner_user_id=OWNER_ID,
        name="result.txt",
        quarantine_object_uri=(
            f"quarantine://tenant/{TENANT_ID}/artifact/{ARTIFACT_ID}/source"
        ),
        object_uri=f"artifact://tenant/{TENANT_ID}/artifact/{ARTIFACT_ID}",
        content_hash="sha256:" + "a" * 64,
        size_bytes=13,
        content_type="text/plain",
        status="AVAILABLE",
        required_output=False,
        scan_result={"decision": "PASSED"},
        upload_expires_at=NOW + timedelta(minutes=15),
        created_at=NOW,
        updated_at=NOW,
        expires_at=NOW + timedelta(days=1),
        deleted_at=None,
    )
    return ArtifactDownloadGrantRecord(
        id=GRANT_ID,
        tenant_id=TENANT_ID,
        artifact_id=ARTIFACT_ID,
        owner_user_id=OWNER_ID,
        token_hash=TOKEN_HASH,
        expires_at=NOW + timedelta(minutes=5),
        revoked_at=None,
        artifact=artifact,
    )


def _service(stub: GatewayStub) -> ArtifactDownloadGatewayService:
    return ArtifactDownloadGatewayService(
        stub,
        stub,
        service_subject_id=SERVICE_ID,
    )


def _metadata() -> RequestMetadata:
    return RequestMetadata(request_id="req-download", trace_id="trace-download")


@pytest.mark.asyncio
async def test_gateway_resolves_token_and_opens_private_tenant_object() -> None:
    stub = GatewayStub()

    content = await _service(stub).open_download(
        grant_id=str(GRANT_ID),
        token=TOKEN,
        range_header=None,
        metadata=_metadata(),
    )

    assert b"".join([chunk async for chunk in content.body]) == b"artifact-body"
    assert stub.context is not None
    assert stub.context.tenant_id == str(TENANT_ID)
    assert stub.context.subject_id == str(SERVICE_ID)


@pytest.mark.asyncio
async def test_gateway_hides_invalid_or_revoked_bearer_capability() -> None:
    stub = GatewayStub()
    service = _service(stub)

    with pytest.raises(PlatformError) as invalid:
        await service.open_download(
            grant_id=str(GRANT_ID),
            token="x" * 43,
            range_header=None,
            metadata=_metadata(),
        )
    assert invalid.value.status_code == 404

    stub.record = replace(stub.record, revoked_at=NOW)
    with pytest.raises(PlatformError) as revoked:
        await service.open_download(
            grant_id=str(GRANT_ID),
            token=TOKEN,
            range_header=None,
            metadata=_metadata(),
        )
    assert revoked.value.status_code == 404


@pytest.mark.asyncio
async def test_gateway_closes_object_when_revocation_wins_open_race() -> None:
    stub = GatewayStub()
    stub.open_allowed = False

    with pytest.raises(PlatformError) as error:
        await _service(stub).open_download(
            grant_id=str(GRANT_ID),
            token=TOKEN,
            range_header=None,
            metadata=_metadata(),
        )

    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_gateway_normalizes_private_object_store_failure() -> None:
    stub = GatewayStub()
    stub.object_unavailable = True

    with pytest.raises(PlatformError) as error:
        await _service(stub).open_download(
            grant_id=str(GRANT_ID),
            token=TOKEN,
            range_header=None,
            metadata=_metadata(),
        )

    assert error.value.code == "DEPENDENCY_UNAVAILABLE"
    assert error.value.retryable is True


@pytest.mark.asyncio
async def test_gateway_resolves_single_range_before_opening_object() -> None:
    stub = GatewayStub()

    content = await _service(stub).open_download(
        grant_id=str(GRANT_ID),
        token=TOKEN,
        range_header="bytes=2-7",
        metadata=_metadata(),
    )

    assert content.byte_range == ArtifactByteRange(start=2, end_inclusive=7)
    assert content.size_bytes == 6
    assert b"".join([chunk async for chunk in content.body]) == b"tifact"


@pytest.mark.asyncio
async def test_gateway_interrupts_open_stream_after_durable_revocation() -> None:
    stub = GatewayStub()
    stub.stream_gate = asyncio.Event()
    subscription = RevocationSubscriptionStub()
    service = ArtifactDownloadGatewayService(
        stub,
        stub,
        service_subject_id=SERVICE_ID,
        revocation_source=RevocationSourceStub(subscription),
        revocation_check_interval=timedelta(milliseconds=10),
    )
    content = await service.open_download(
        grant_id=str(GRANT_ID),
        token=TOKEN,
        range_header=None,
        metadata=_metadata(),
    )
    stream = content.body.__aiter__()

    assert await anext(stream) == b"arti"
    stub.active = False
    subscription.event.set()
    with pytest.raises(StopAsyncIteration):
        await anext(stream)

    assert stub.closed is True
    assert subscription.closed is True


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("bytes=2-7", ArtifactByteRange(start=2, end_inclusive=7)),
        ("bytes=8-", ArtifactByteRange(start=8, end_inclusive=12)),
        ("bytes=-4", ArtifactByteRange(start=9, end_inclusive=12)),
        ("bytes=10-99", ArtifactByteRange(start=10, end_inclusive=12)),
    ],
)
def test_single_range_resolution(header: str, expected: ArtifactByteRange) -> None:
    assert resolve_artifact_byte_range(header, total_size=13) == expected


@pytest.mark.parametrize(
    "header",
    ["bytes=", "bytes=7-2", "bytes=13-", "bytes=0-1,3-4", "items=0-1"],
)
def test_invalid_or_unsatisfied_range_returns_frozen_416(header: str) -> None:
    with pytest.raises(PlatformError) as error:
        resolve_artifact_byte_range(header, total_size=13)

    assert error.value.status_code == 416
    assert error.value.code == "RANGE_NOT_SATISFIABLE"
    assert error.value.headers == {"Content-Range": "bytes */13"}

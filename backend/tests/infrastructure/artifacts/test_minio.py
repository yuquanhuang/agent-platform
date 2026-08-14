"""MinIO Artifact adapter unit contract tests."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from uuid import UUID

import pytest
from minio import Minio
from minio.commonconfig import CopySource
from minio.datatypes import Object
from pydantic import SecretStr

from packages.application.artifacts import (
    ArtifactByteRange,
    ArtifactDownloadCapacityExceeded,
)
from packages.contracts.public import SubjectType, TenantContext
from packages.domain.public import ArtifactRecord
from packages.infrastructure.artifacts import MinioArtifactObjectStore

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ARTIFACT_ID = UUID("22222222-2222-4222-8222-222222222222")
USER_ID = UUID("33333333-3333-4333-8333-333333333333")
PAYLOAD = b"artifact-body"


class ResponseStub:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self._offset = 0
        self.closed = False

    def read(self, size: int) -> bytes:
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.closed = True


class ClosableAsyncIterator(Protocol):
    async def aclose(self) -> None: ...


class MinioStub:
    def __init__(self) -> None:
        self.objects = {
            f"quarantine/{TENANT_ID}/{ARTIFACT_ID}/source": PAYLOAD,
        }
        self.content_types = {
            f"quarantine/{TENANT_ID}/{ARTIFACT_ID}/source": "text/plain",
        }
        self.removed: list[str] = []

    def bucket_exists(self, bucket: str) -> bool:
        return bucket == "agent-platform"

    def presigned_put_object(self, bucket: str, key: str, expires: timedelta) -> str:
        assert bucket == "agent-platform"
        assert expires.total_seconds() > 0
        return f"https://minio.example.test/{bucket}/{key}?signature=opaque"

    def stat_object(self, bucket: str, key: str) -> Object:
        assert bucket == "agent-platform"
        body = self.objects[key]
        return Object(
            bucket,
            key,
            size=len(body),
            content_type=self.content_types[key],
        )

    def get_object(
        self,
        bucket: str,
        key: str,
        offset: int = 0,
        length: int = 0,
    ) -> ResponseStub:
        assert bucket == "agent-platform"
        body = self.objects[key][offset:]
        if length:
            body = body[:length]
        return ResponseStub(body)

    def copy_object(self, bucket: str, target: str, source: CopySource) -> object:
        assert bucket == "agent-platform"
        source_key = source.object_name
        self.objects[target] = self.objects[source_key]
        self.content_types[target] = self.content_types[source_key]
        return object()

    def remove_object(self, bucket: str, key: str) -> None:
        assert bucket == "agent-platform"
        self.removed.append(key)
        self.objects.pop(key, None)
        self.content_types.pop(key, None)


def _context(tenant_id: UUID = TENANT_ID) -> TenantContext:
    return TenantContext(
        tenant_id=str(tenant_id),
        subject_type=SubjectType.SERVICE,
        subject_id=str(USER_ID),
        auth_time=datetime.now(UTC),
        request_id="req-minio",
        trace_id="trace-minio",
    )


def _artifact(*, trusted: bool = False) -> ArtifactRecord:
    now = datetime.now(UTC)
    return ArtifactRecord(
        id=ARTIFACT_ID,
        tenant_id=TENANT_ID,
        workspace_id=None,
        run_id=None,
        owner_user_id=USER_ID,
        name="result.txt",
        quarantine_object_uri=(
            f"quarantine://tenant/{TENANT_ID}/artifact/{ARTIFACT_ID}/source"
        ),
        object_uri=(
            f"artifact://tenant/{TENANT_ID}/artifact/{ARTIFACT_ID}" if trusted else None
        ),
        content_hash="sha256:" + hashlib.sha256(PAYLOAD).hexdigest(),
        size_bytes=len(PAYLOAD),
        content_type="text/plain",
        status="AVAILABLE" if trusted else "UPLOADING",
        required_output=False,
        scan_result=None,
        upload_expires_at=now + timedelta(minutes=15),
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(days=1),
        retention_delete_after=None,
        deleted_at=None,
    )


def _adapter(
    stub: MinioStub,
    *,
    max_concurrent_downloads: int = 32,
    download_acquire_timeout_seconds: float = 1.0,
) -> MinioArtifactObjectStore:
    return MinioArtifactObjectStore(
        cast(Minio, stub),
        bucket="agent-platform",
        max_concurrent_downloads=max_concurrent_downloads,
        download_acquire_timeout_seconds=download_acquire_timeout_seconds,
    )


@pytest.mark.asyncio
async def test_minio_adapter_upload_inspect_promote_and_range_read() -> None:
    stub = MinioStub()
    adapter = _adapter(stub)
    artifact = _artifact()

    await adapter.check_ready()
    grant = await adapter.create_upload_grant(_context(), artifact=artifact)
    observation = await adapter.inspect_quarantine(_context(), artifact=artifact)
    object_uri = await adapter.promote(_context(), artifact=artifact)
    content = await adapter.open_trusted_artifact(
        _context(),
        artifact=_artifact(trusted=True),
        byte_range=ArtifactByteRange(start=2, end_inclusive=7),
    )

    assert grant.required_headers == {"Content-Type": "text/plain"}
    assert observation.size_bytes == len(PAYLOAD)
    assert observation.content_hash == "sha256:" + hashlib.sha256(PAYLOAD).hexdigest()
    assert object_uri == f"artifact://tenant/{TENANT_ID}/artifact/{ARTIFACT_ID}"
    assert b"".join([chunk async for chunk in content.body]) == PAYLOAD[2:8]


@pytest.mark.asyncio
async def test_minio_adapter_enforces_tenant_and_canonical_keys() -> None:
    adapter = _adapter(MinioStub())

    with pytest.raises(ValueError, match="does not match"):
        await adapter.inspect_quarantine(
            _context(UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")),
            artifact=_artifact(),
        )


@pytest.mark.asyncio
async def test_minio_adapter_bounds_concurrent_downloads() -> None:
    stub = MinioStub()
    adapter = _adapter(
        stub,
        max_concurrent_downloads=1,
        download_acquire_timeout_seconds=0.01,
    )
    await adapter.promote(_context(), artifact=_artifact())
    first = await adapter.open_trusted_artifact(
        _context(), artifact=_artifact(trusted=True), byte_range=None
    )

    with pytest.raises(ArtifactDownloadCapacityExceeded):
        await adapter.open_trusted_artifact(
            _context(), artifact=_artifact(trusted=True), byte_range=None
        )

    await cast(ClosableAsyncIterator, first.body).aclose()


@pytest.mark.asyncio
async def test_minio_adapter_deletes_quarantine_and_trusted_objects() -> None:
    stub = MinioStub()
    adapter = _adapter(stub)
    artifact = _artifact()
    await adapter.promote(_context(), artifact=artifact)

    await adapter.delete_artifact_objects(_context(), artifact=_artifact(trusted=True))

    assert stub.removed == [
        f"quarantine/{TENANT_ID}/{ARTIFACT_ID}/source",
        f"trusted/{TENANT_ID}/{ARTIFACT_ID}/content",
    ]


def test_minio_credentials_and_endpoint_fail_closed() -> None:
    credentials = SecretStr('{"access_key":"minio-admin","secret_key":"minio-secret"}')
    adapter = MinioArtifactObjectStore.from_secret(
        endpoint_url="https://minio.example.test",
        bucket="agent-platform",
        credential=credentials,
    )

    assert isinstance(adapter, MinioArtifactObjectStore)
    with pytest.raises(ValueError, match="without credentials"):
        MinioArtifactObjectStore.from_secret(
            endpoint_url="https://user:password@minio.example.test/path",
            bucket="agent-platform",
            credential=credentials,
        )
    with pytest.raises(ValueError, match="credential secret"):
        MinioArtifactObjectStore.from_secret(
            endpoint_url="https://minio.example.test",
            bucket="agent-platform",
            credential=SecretStr("not-json"),
        )

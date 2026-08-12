"""Encrypted MinIO AgentScope checkpoint State Store tests."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import BinaryIO, cast
from uuid import UUID

import pytest
from minio import Minio
from pydantic import SecretStr

from packages.application.temporal import RunExecutionRequest
from packages.contracts.public import SubjectType, TenantContext
from packages.contracts.temporal import RunSpecReference
from packages.infrastructure.artifacts import MinioAgentScopeStateStore
from packages.runtimes.agentscope import AgentScopeCheckpointRecord

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
RUN_ID = UUID("22222222-2222-4222-8222-222222222222")
CHECKPOINT_ID = UUID("33333333-3333-4333-8333-333333333333")
STATE = b'{"session_id":"agentscope-session"}'


class ResponseStub:
    def __init__(self, value: bytes) -> None:
        self._value = value
        self._offset = 0

    def read(self, size: int) -> bytes:
        chunk = self._value[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None: ...

    def release_conn(self) -> None: ...


class MinioStub:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_put = False

    def put_object(
        self,
        bucket: str,
        key: str,
        data: BinaryIO,
        length: int,
        **kwargs: object,
    ) -> object:
        assert bucket == "agent-platform"
        if self.fail_put:
            raise OSError("MinIO unavailable")
        value = data.read(length)
        assert len(value) == length
        self.objects[key] = value
        return object()

    def get_object(self, bucket: str, key: str) -> ResponseStub:
        assert bucket == "agent-platform"
        return ResponseStub(self.objects[key])


class MetadataStub:
    def __init__(self) -> None:
        self.record: AgentScopeCheckpointRecord | None = None
        self.failed = False

    async def reserve(self, context: TenantContext, **kwargs: object):
        now = datetime.now(UTC)
        self.record = AgentScopeCheckpointRecord(
            id=CHECKPOINT_ID,
            tenant_id=TENANT_ID,
            run_id=RUN_ID,
            execution_attempt=1,
            sequence_no=1,
            state_ref=(
                f"state://tenant/{TENANT_ID}/run/{RUN_ID}/"
                f"attempt/1/checkpoint/{CHECKPOINT_ID}"
            ),
            object_key=f"runtime-checkpoints/{CHECKPOINT_ID}.json",
            content_hash=cast(str, kwargs["content_hash"]),
            size_bytes=cast(int, kwargs["size_bytes"]),
            fencing_token_hash=cast(str, kwargs["fencing_token_hash"]),
            status="PENDING",
            created_at=now,
            available_at=None,
            expires_at=cast(datetime, kwargs["expires_at"]),
        )
        return self.record

    async def mark_available(self, context: TenantContext, **kwargs: object):
        assert self.record is not None
        self.record = replace(
            self.record,
            status="AVAILABLE",
            available_at=cast(datetime, kwargs["available_at"]),
        )
        return self.record

    async def mark_failed(self, context: TenantContext, **kwargs: object) -> None:
        self.failed = True
        if self.record is not None:
            self.record = replace(self.record, status="FAILED")

    async def load_latest(self, context: TenantContext, **kwargs: object):
        if self.record is None or self.record.status != "AVAILABLE":
            return None
        return self.record


def _request() -> RunExecutionRequest:
    return RunExecutionRequest(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        execution_attempt=1,
        run_spec=RunSpecReference(
            uri="memory://run-spec/1",
            content_hash="sha256:" + "a" * 64,
            size_bytes=1024,
        ),
        timeout_seconds=600,
        runtime_type="agentscope",
        fencing_token=SecretStr("fencing-token-for-checkpoint"),
    )


def _context() -> TenantContext:
    return TenantContext(
        tenant_id=str(TENANT_ID),
        subject_type=SubjectType.SERVICE,
        subject_id=str(RUN_ID),
        auth_time=datetime.now(UTC),
        request_id="req-checkpoint",
        trace_id="trace-checkpoint",
    )


def _store(client: MinioStub, metadata: MetadataStub) -> MinioAgentScopeStateStore:
    key = base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode()
    return MinioAgentScopeStateStore(
        cast(Minio, client),
        cast(object, metadata),  # type: ignore[arg-type]
        bucket="agent-platform",
        encryption_key=SecretStr(key),
        retention=timedelta(hours=1),
    )


@pytest.mark.asyncio
async def test_checkpoint_is_encrypted_bound_and_restorable() -> None:
    client = MinioStub()
    metadata = MetadataStub()
    store = _store(client, metadata)

    state_ref = await store.save(_context(), request=_request(), state_json=STATE)
    restored = await store.load_latest(_context(), request=_request())

    assert state_ref.endswith(str(CHECKPOINT_ID))
    encrypted = next(iter(client.objects.values()))
    assert STATE not in encrypted
    assert restored == STATE
    assert metadata.record is not None
    assert metadata.record.content_hash == "sha256:" + hashlib.sha256(STATE).hexdigest()


@pytest.mark.asyncio
async def test_checkpoint_tampering_fails_closed() -> None:
    client = MinioStub()
    metadata = MetadataStub()
    store = _store(client, metadata)
    await store.save(_context(), request=_request(), state_json=STATE)
    key = next(iter(client.objects))
    client.objects[key] = client.objects[key][:-1] + b"x"

    with pytest.raises(RuntimeError, match="decryption"):
        await store.load_latest(_context(), request=_request())


@pytest.mark.asyncio
async def test_checkpoint_upload_failure_marks_metadata_failed() -> None:
    client = MinioStub()
    client.fail_put = True
    metadata = MetadataStub()

    with pytest.raises(OSError):
        await _store(client, metadata).save(
            _context(), request=_request(), state_json=STATE
        )

    assert metadata.failed is True

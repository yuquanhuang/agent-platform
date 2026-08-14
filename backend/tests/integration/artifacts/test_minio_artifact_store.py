"""Opt-in real MinIO verification for the production Artifact adapter."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import UUID, uuid4

import httpx
import pytest
from minio import Minio
from minio.error import S3Error
from pydantic import SecretStr

from packages.application.artifacts import (
    ArtifactByteRange,
    ArtifactObjectStoreUnavailable,
)
from packages.contracts.public import SubjectType, TenantContext
from packages.domain.public import ArtifactRecord
from packages.infrastructure.artifacts import MinioArtifactObjectStore

MINIO_ENDPOINT_ENV = "AP_TEST_MINIO_ENDPOINT"
MINIO_ACCESS_KEY_ENV = "AP_TEST_MINIO_ACCESS_KEY"
MINIO_SECRET_KEY_ENV = "AP_TEST_MINIO_SECRET_KEY"
MINIO_BUCKET_ENV = "AP_TEST_MINIO_BUCKET"
TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
USER_ID = UUID("22222222-2222-4222-8222-222222222222")
PAYLOAD = b"real-minio-artifact-body"


@pytest.mark.asyncio
async def test_real_minio_artifact_lifecycle_and_range() -> None:
    endpoint = os.getenv(MINIO_ENDPOINT_ENV)
    access_key = os.getenv(MINIO_ACCESS_KEY_ENV)
    secret_key = os.getenv(MINIO_SECRET_KEY_ENV)
    bucket = os.getenv(MINIO_BUCKET_ENV, "agent-platform-test")
    if endpoint is None or access_key is None or secret_key is None:
        pytest.skip(
            f"{MINIO_ENDPOINT_ENV}, {MINIO_ACCESS_KEY_ENV} and "
            f"{MINIO_SECRET_KEY_ENV} are required"
        )
    credentials = SecretStr(
        json.dumps({"access_key": access_key, "secret_key": secret_key})
    )
    adapter = MinioArtifactObjectStore.from_secret(
        endpoint_url=endpoint,
        bucket=bucket,
        credential=credentials,
    )
    raw_client = Minio(
        endpoint.removeprefix("http://").removeprefix("https://"),
        access_key=access_key,
        secret_key=secret_key,
        secure=endpoint.startswith("https://"),
    )
    created_bucket = False
    if not raw_client.bucket_exists(bucket):
        raw_client.make_bucket(bucket)
        created_bucket = True
    artifact = _artifact(uuid4())
    context = _context()
    try:
        await adapter.check_ready()
        grant = await adapter.create_upload_grant(context, artifact=artifact)
        async with httpx.AsyncClient(timeout=10) as client:
            upload = await client.put(
                grant.upload_url,
                content=PAYLOAD,
                headers=grant.required_headers,
            )
        assert upload.status_code == 200

        observation = await adapter.inspect_quarantine(context, artifact=artifact)
        assert observation.size_bytes == len(PAYLOAD)
        assert observation.content_hash == artifact.content_hash
        copied = BytesIO()
        assert await adapter.copy_quarantine(
            context,
            artifact=artifact,
            destination=copied,
            max_bytes=1024,
        ) == len(PAYLOAD)
        assert copied.read() == PAYLOAD

        trusted_uri = await adapter.promote(context, artifact=artifact)
        trusted = replace(artifact, object_uri=trusted_uri, status="AVAILABLE")
        content = await adapter.open_trusted_artifact(
            context,
            artifact=trusted,
            byte_range=ArtifactByteRange(start=5, end_inclusive=9),
        )
        assert b"".join([chunk async for chunk in content.body]) == PAYLOAD[5:10]

        await adapter.delete_artifact_objects(context, artifact=trusted)
        with pytest.raises(S3Error) as deleted:
            raw_client.stat_object(
                bucket,
                f"trusted/{TENANT_ID}/{artifact.id}/content",
            )
        assert deleted.value.code in {"NoSuchKey", "NoSuchObject"}
    finally:
        for key in (
            f"quarantine/{TENANT_ID}/{artifact.id}/source",
            f"trusted/{TENANT_ID}/{artifact.id}/content",
        ):
            raw_client.remove_object(bucket, key)
        if created_bucket:
            raw_client.remove_bucket(bucket)


@pytest.mark.asyncio
async def test_real_minio_outage_fails_closed() -> None:
    if os.getenv("AP_TEST_MINIO_EXPECT_UNAVAILABLE") != "1":
        pytest.skip("AP_TEST_MINIO_EXPECT_UNAVAILABLE=1 is required")
    endpoint = os.getenv(MINIO_ENDPOINT_ENV)
    access_key = os.getenv(MINIO_ACCESS_KEY_ENV)
    secret_key = os.getenv(MINIO_SECRET_KEY_ENV)
    if endpoint is None or access_key is None or secret_key is None:
        pytest.skip("Real MinIO test configuration is required")
    adapter = MinioArtifactObjectStore.from_secret(
        endpoint_url=endpoint,
        bucket=os.getenv(MINIO_BUCKET_ENV, "agent-platform-test"),
        credential=SecretStr(
            json.dumps({"access_key": access_key, "secret_key": secret_key})
        ),
        connect_timeout_seconds=0.2,
        read_timeout_seconds=0.2,
    )

    with pytest.raises(ArtifactObjectStoreUnavailable):
        await adapter.check_ready()


def _context() -> TenantContext:
    return TenantContext(
        tenant_id=str(TENANT_ID),
        subject_type=SubjectType.SERVICE,
        subject_id=str(USER_ID),
        auth_time=datetime.now(UTC),
        request_id="req-real-minio",
        trace_id="trace-real-minio",
    )


def _artifact(artifact_id: UUID) -> ArtifactRecord:
    now = datetime.now(UTC)
    return ArtifactRecord(
        id=artifact_id,
        tenant_id=TENANT_ID,
        workspace_id=None,
        run_id=None,
        owner_user_id=USER_ID,
        name="result.txt",
        quarantine_object_uri=(
            f"quarantine://tenant/{TENANT_ID}/artifact/{artifact_id}/source"
        ),
        object_uri=None,
        content_hash="sha256:" + hashlib.sha256(PAYLOAD).hexdigest(),
        size_bytes=len(PAYLOAD),
        content_type="application/octet-stream",
        status="SCANNING",
        required_output=False,
        scan_result=None,
        upload_expires_at=now + timedelta(minutes=15),
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(days=1),
        retention_delete_after=None,
        deleted_at=None,
    )

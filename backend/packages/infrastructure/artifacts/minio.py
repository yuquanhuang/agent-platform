"""MinIO/S3-compatible Artifact object storage adapter."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from json import JSONDecodeError
from typing import BinaryIO, Self
from urllib.parse import urlsplit
from uuid import UUID

import urllib3
from minio import Minio
from minio.commonconfig import CopySource
from minio.datatypes import Object
from minio.error import S3Error
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from urllib3.response import BaseHTTPResponse
from urllib3.util import Timeout

from packages.application.artifacts import (
    ArtifactByteRange,
    ArtifactDownloadCapacityExceeded,
    ArtifactDownloadObjectNotFound,
    ArtifactDownloadObjectUnavailable,
    ArtifactObjectNotFound,
    ArtifactObjectObservation,
    ArtifactObjectStoreUnavailable,
    ArtifactTrustedContent,
    ArtifactUploadGrant,
    PermanentArtifactScanError,
    RetryableArtifactDeleteError,
    RetryableArtifactScanError,
)
from packages.contracts.public import TenantContext
from packages.domain.public import ArtifactRecord, artifact_uri

_NOT_FOUND_CODES = frozenset({"NoSuchKey", "NoSuchObject", "NoSuchBucket"})
_BUCKET_PATTERN = re.compile(
    r"^(?!\d+\.\d+\.\d+\.\d+$)(?!.*\.\.)(?!.*\.-|.*-\.)"
    r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"
)


class MinioCredentialPayload(BaseModel):
    """One Kubernetes-injected JSON secret for MinIO credentials."""

    model_config = ConfigDict(extra="forbid")

    access_key: str = Field(min_length=3, max_length=128)
    secret_key: SecretStr = Field(min_length=8, max_length=1024)
    session_token: SecretStr | None = Field(default=None, max_length=4096)


class MinioArtifactObjectStore:
    """One tenant-isolated adapter implementing all Artifact object ports."""

    def __init__(
        self,
        client: Minio,
        *,
        bucket: str,
        download_chunk_bytes: int = 1_048_576,
        max_concurrent_downloads: int = 32,
        download_acquire_timeout_seconds: float = 1.0,
    ) -> None:
        if _BUCKET_PATTERN.fullmatch(bucket) is None:
            raise ValueError("MinIO bucket name is invalid")
        if not 65_536 <= download_chunk_bytes <= 8_388_608:
            raise ValueError("MinIO download chunk size is invalid")
        if max_concurrent_downloads < 1:
            raise ValueError("MinIO download concurrency must be positive")
        if download_acquire_timeout_seconds <= 0:
            raise ValueError("MinIO download acquire timeout must be positive")
        self._client = client
        self._bucket = bucket
        self._download_chunk_bytes = download_chunk_bytes
        self._download_slots = asyncio.Semaphore(max_concurrent_downloads)
        self._download_acquire_timeout_seconds = download_acquire_timeout_seconds

    @classmethod
    def from_secret(
        cls,
        *,
        endpoint_url: str,
        bucket: str,
        credential: SecretStr,
        region: str | None = None,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 30.0,
        download_chunk_bytes: int = 1_048_576,
        max_concurrent_downloads: int = 32,
        download_acquire_timeout_seconds: float = 1.0,
    ) -> Self:
        split = urlsplit(endpoint_url)
        if (
            split.scheme not in {"http", "https"}
            or not split.netloc
            or split.username is not None
            or split.password is not None
            or split.path not in {"", "/"}
            or split.query
            or split.fragment
        ):
            raise ValueError("MinIO endpoint must be an origin without credentials")
        if connect_timeout_seconds <= 0 or read_timeout_seconds <= 0:
            raise ValueError("MinIO timeouts must be positive")
        credentials = _parse_credentials(credential)
        http_client = urllib3.PoolManager(
            timeout=Timeout(
                connect=connect_timeout_seconds,
                read=read_timeout_seconds,
            ),
            retries=False,
            cert_reqs="CERT_REQUIRED",
        )
        client = Minio(
            split.netloc,
            access_key=credentials.access_key,
            secret_key=credentials.secret_key.get_secret_value(),
            session_token=(
                credentials.session_token.get_secret_value()
                if credentials.session_token is not None
                else None
            ),
            secure=split.scheme == "https",
            region=region,
            http_client=http_client,
            cert_check=True,
        )
        return cls(
            client,
            bucket=bucket,
            download_chunk_bytes=download_chunk_bytes,
            max_concurrent_downloads=max_concurrent_downloads,
            download_acquire_timeout_seconds=(download_acquire_timeout_seconds),
        )

    async def check_ready(self) -> None:
        try:
            exists = await asyncio.to_thread(self._client.bucket_exists, self._bucket)
        except Exception as error:
            raise ArtifactObjectStoreUnavailable(
                "MinIO readiness check failed"
            ) from error
        if not exists:
            raise ArtifactObjectStoreUnavailable(
                "The configured MinIO bucket does not exist"
            )

    async def create_upload_grant(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> ArtifactUploadGrant:
        key = _quarantine_key(context, artifact)
        now = datetime.now(UTC)
        expires = artifact.upload_expires_at - now
        if expires.total_seconds() <= 0:
            raise ArtifactObjectStoreUnavailable("Artifact upload window has expired")
        try:
            upload_url = await asyncio.to_thread(
                self._client.presigned_put_object,
                self._bucket,
                key,
                expires,
            )
        except Exception as error:
            raise ArtifactObjectStoreUnavailable(
                "MinIO upload authorization failed"
            ) from error
        return ArtifactUploadGrant(
            upload_url=upload_url,
            expires_at=artifact.upload_expires_at,
            required_headers={"Content-Type": artifact.content_type},
        )

    async def inspect_quarantine(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> ArtifactObjectObservation:
        key = _quarantine_key(context, artifact)
        try:
            stat = await asyncio.to_thread(self._client.stat_object, self._bucket, key)
            digest = await asyncio.to_thread(
                self._hash_object,
                key,
                artifact.size_bytes,
            )
        except S3Error as error:
            if error.code in _NOT_FOUND_CODES:
                raise ArtifactObjectNotFound(
                    "MinIO quarantine object is missing"
                ) from error
            raise ArtifactObjectStoreUnavailable(
                "MinIO quarantine inspection failed"
            ) from error
        except Exception as error:
            raise ArtifactObjectStoreUnavailable(
                "MinIO quarantine inspection failed"
            ) from error
        return ArtifactObjectObservation(
            size_bytes=_required_size(stat),
            content_hash=f"sha256:{digest}",
            content_type=stat.content_type or "application/octet-stream",
        )

    async def copy_quarantine(
        self,
        context: TenantContext,
        *,
        artifact: ArtifactRecord,
        destination: BinaryIO,
        max_bytes: int,
    ) -> int:
        key = _quarantine_key(context, artifact)
        try:
            return await asyncio.to_thread(
                self._copy_object_to_file,
                key,
                destination,
                max_bytes,
            )
        except S3Error as error:
            raise RetryableArtifactScanError(
                "MinIO quarantine content is unavailable"
            ) from error
        except Exception as error:
            raise RetryableArtifactScanError(
                "MinIO quarantine content is unavailable"
            ) from error

    async def promote(self, context: TenantContext, *, artifact: ArtifactRecord) -> str:
        source = _quarantine_key(context, artifact)
        target = _trusted_key(context, artifact)
        try:
            await asyncio.to_thread(
                self._client.copy_object,
                self._bucket,
                target,
                CopySource(self._bucket, source),
            )
            stat = await asyncio.to_thread(
                self._client.stat_object, self._bucket, target
            )
        except S3Error as error:
            raise RetryableArtifactScanError(
                "MinIO trusted promotion failed"
            ) from error
        except Exception as error:
            raise RetryableArtifactScanError(
                "MinIO trusted promotion failed"
            ) from error
        if _required_size(stat) != artifact.size_bytes:
            raise PermanentArtifactScanError(
                "MinIO trusted object size drifted during promotion"
            )
        return artifact_uri(tenant_id=artifact.tenant_id, artifact_id=artifact.id)

    async def revoke_download_access(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> None:
        _trusted_key(context, artifact)
        # Downloads never use MinIO presigned URLs. Durable database grant
        # revocation is therefore the only capability revocation boundary.

    async def delete_artifact_objects(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> None:
        keys = (_quarantine_key(context, artifact), _trusted_key(context, artifact))
        try:
            for key in keys:
                await asyncio.to_thread(self._client.remove_object, self._bucket, key)
        except Exception as error:
            raise RetryableArtifactDeleteError(
                "MinIO Artifact cleanup failed"
            ) from error

    async def open_trusted_artifact(
        self,
        context: TenantContext,
        *,
        artifact: ArtifactRecord,
        byte_range: ArtifactByteRange | None,
    ) -> ArtifactTrustedContent:
        key = _trusted_key(context, artifact)
        try:
            stat = await asyncio.to_thread(self._client.stat_object, self._bucket, key)
            total_size = _required_size(stat)
            if total_size != artifact.size_bytes:
                raise ArtifactDownloadObjectUnavailable(
                    "MinIO trusted object size is inconsistent"
                )
            body = await _MinioAsyncBody.open(
                self._client,
                bucket=self._bucket,
                key=key,
                byte_range=byte_range,
                semaphore=self._download_slots,
                acquire_timeout_seconds=(self._download_acquire_timeout_seconds),
                chunk_bytes=self._download_chunk_bytes,
            )
        except S3Error as error:
            if error.code in _NOT_FOUND_CODES:
                raise ArtifactDownloadObjectNotFound(
                    "MinIO trusted object is missing"
                ) from error
            raise ArtifactDownloadObjectUnavailable(
                "MinIO trusted object cannot be opened"
            ) from error
        except (ArtifactDownloadCapacityExceeded, ArtifactDownloadObjectUnavailable):
            raise
        except Exception as error:
            raise ArtifactDownloadObjectUnavailable(
                "MinIO trusted object cannot be opened"
            ) from error
        return ArtifactTrustedContent(
            body=body,
            size_bytes=(byte_range.length if byte_range is not None else total_size),
            total_size_bytes=total_size,
            content_type=stat.content_type or artifact.content_type,
            name=artifact.name,
            byte_range=byte_range,
        )

    def _hash_object(self, key: str, expected_size: int) -> str:
        response = self._client.get_object(self._bucket, key)
        digest = hashlib.sha256()
        total = 0
        try:
            while True:
                chunk = response.read(self._download_chunk_bytes)
                if not chunk:
                    break
                total += len(chunk)
                if total > expected_size:
                    break
                digest.update(chunk)
        finally:
            response.close()
            response.release_conn()
        if total != expected_size:
            raise ArtifactObjectStoreUnavailable(
                "MinIO object size changed during inspection"
            )
        return digest.hexdigest()

    def _copy_object_to_file(
        self,
        key: str,
        destination: BinaryIO,
        max_bytes: int,
    ) -> int:
        if max_bytes < 1:
            raise ValueError("Artifact copy maximum must be positive")
        response = self._client.get_object(self._bucket, key)
        total = 0
        try:
            while total <= max_bytes:
                chunk = response.read(
                    min(self._download_chunk_bytes, max_bytes + 1 - total)
                )
                if not chunk:
                    break
                destination.write(chunk)
                total += len(chunk)
        finally:
            response.close()
            response.release_conn()
        destination.seek(0)
        return total


class _MinioAsyncBody(AsyncIterator[bytes]):
    def __init__(
        self,
        response: BaseHTTPResponse,
        semaphore: asyncio.Semaphore,
        *,
        chunk_bytes: int,
    ) -> None:
        self._response = response
        self._semaphore = semaphore
        self._chunk_bytes = chunk_bytes
        self._closed = False

    @classmethod
    async def open(
        cls,
        client: Minio,
        *,
        bucket: str,
        key: str,
        byte_range: ArtifactByteRange | None,
        semaphore: asyncio.Semaphore,
        acquire_timeout_seconds: float,
        chunk_bytes: int,
    ) -> Self:
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=acquire_timeout_seconds)
        except TimeoutError as error:
            raise ArtifactDownloadCapacityExceeded(
                "MinIO download concurrency is exhausted"
            ) from error
        try:
            response = await asyncio.to_thread(
                client.get_object,
                bucket,
                key,
                byte_range.start if byte_range is not None else 0,
                byte_range.length if byte_range is not None else 0,
            )
        except BaseException:
            semaphore.release()
            raise
        return cls(response, semaphore, chunk_bytes=chunk_bytes)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> bytes:
        if self._closed:
            raise StopAsyncIteration
        try:
            chunk = await asyncio.to_thread(self._response.read, self._chunk_bytes)
        except BaseException as error:
            await self.aclose()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise ArtifactDownloadObjectUnavailable(
                "MinIO download stream failed"
            ) from error
        if chunk:
            return chunk
        await self.aclose()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await asyncio.to_thread(self._response.close)
            await asyncio.to_thread(self._response.release_conn)
        finally:
            self._semaphore.release()


def _parse_credentials(secret: SecretStr) -> MinioCredentialPayload:
    try:
        return MinioCredentialPayload.model_validate_json(secret.get_secret_value())
    except (ValidationError, JSONDecodeError) as error:
        raise ValueError("MinIO credential secret is invalid") from error


def _quarantine_key(context: TenantContext, artifact: ArtifactRecord) -> str:
    tenant_id = _tenant_id(context, artifact)
    expected_uri = f"quarantine://tenant/{tenant_id}/artifact/{artifact.id}/source"
    if artifact.quarantine_object_uri != expected_uri:
        raise ValueError("Artifact quarantine URI is not canonical")
    return f"quarantine/{tenant_id}/{artifact.id}/source"


def _trusted_key(context: TenantContext, artifact: ArtifactRecord) -> str:
    tenant_id = _tenant_id(context, artifact)
    expected_uri = artifact_uri(tenant_id=tenant_id, artifact_id=artifact.id)
    if artifact.object_uri is not None and artifact.object_uri != expected_uri:
        raise ValueError("Artifact trusted URI is not canonical")
    return f"trusted/{tenant_id}/{artifact.id}/content"


def _tenant_id(context: TenantContext, artifact: ArtifactRecord) -> UUID:
    try:
        context_tenant_id = UUID(context.tenant_id)
    except ValueError as error:
        raise ValueError("Tenant context identity is invalid") from error
    if context_tenant_id != artifact.tenant_id:
        raise ValueError("Artifact tenant does not match TenantContext")
    return context_tenant_id


def _required_size(stat: Object) -> int:
    if stat.size is None or stat.size < 0:
        raise ArtifactObjectStoreUnavailable("MinIO object size is unavailable")
    return stat.size

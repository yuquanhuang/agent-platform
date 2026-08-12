"""Frozen Artifact upload, metadata and completion route tests."""

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID

import httpx
import pytest

from apps.api.app import create_app
from packages.application.public import (
    ArtifactByteRange,
    ArtifactDownloadGatewayService,
    ArtifactManagementService,
    ArtifactTrustedContent,
)
from packages.contracts.generated.core_models import (
    Artifact,
    ArtifactDownload,
    ArtifactUploadAccepted,
    OperationAccepted,
)
from packages.infrastructure.auth.public import MockIdentityProvider
from packages.infrastructure.public import AppSettings

ARTIFACT_ID = UUID("33333333-3333-4333-8333-333333333333")
NOW = datetime.now(UTC)
HASH = "sha256:" + "a" * 64


class ArtifactServiceStub:
    async def create_upload(self, principal: object, **kwargs: object):
        return ArtifactUploadAccepted(
            artifact_id=str(ARTIFACT_ID),
            upload_url="https://objects.test/upload?signature=opaque",
            expires_at=NOW + timedelta(minutes=5),
            required_headers={"Content-Type": "text/plain"},
        )

    async def get_artifact(self, principal: object, **kwargs: object):
        return _artifact("UPLOADING")

    async def complete_upload(self, principal: object, **kwargs: object):
        return _artifact("SCANNING")

    async def create_download(self, principal: object, **kwargs: object):
        return ArtifactDownload(
            url="https://objects.test/download?signature=opaque",
            expires_at=NOW + timedelta(minutes=5),
        )

    async def delete_artifact(self, principal: object, **kwargs: object):
        return OperationAccepted(
            operation_id="44444444-4444-4444-8444-444444444444",
            status="ACCEPTED",
            status_url=("/api/v1/operations/44444444-4444-4444-8444-444444444444"),
        )


class ArtifactDownloadGatewayStub:
    async def open_download(self, **kwargs: object) -> ArtifactTrustedContent:
        requested_range = kwargs.get("range_header")

        async def body():
            yield b"artifact" if requested_range is not None else b"artifact-body"

        return ArtifactTrustedContent(
            body=body(),
            size_bytes=8 if requested_range is not None else 13,
            total_size_bytes=13,
            content_type="text/plain",
            name="result.txt",
            byte_range=(
                ArtifactByteRange(start=0, end_inclusive=7)
                if requested_range is not None
                else None
            ),
        )


def _artifact(status: str) -> Artifact:
    return Artifact(
        id=str(ARTIFACT_ID),
        name="result.txt",
        status=cast(
            Literal[
                "UPLOADING",
                "SCANNING",
                "AVAILABLE",
                "REJECTED",
                "FAILED",
                "EXPIRED",
                "DELETING",
                "DELETED",
            ],
            status,
        ),
        size=12,
        content_type="text/plain",
        hash=HASH,
        run_id=None,
        expires_at=NOW + timedelta(days=30),
        created_at=NOW,
    )


def build_app():
    settings = AppSettings()
    return create_app(
        settings,
        identity_provider=MockIdentityProvider(settings, now=lambda: NOW),
        artifact_service=cast(ArtifactManagementService, ArtifactServiceStub()),
        artifact_download_gateway=cast(
            ArtifactDownloadGatewayService, ArtifactDownloadGatewayStub()
        ),
    )


def test_app_registers_all_frozen_artifact_operations() -> None:
    operation_ids = set(
        re.findall(r'"operationId":\s*"([^"]+)"', json.dumps(build_app().openapi()))
    )

    assert {
        "createArtifactUpload",
        "getArtifact",
        "completeArtifactUpload",
        "createArtifactDownload",
        "downloadArtifactContent",
        "deleteArtifact",
    } <= operation_ids


@pytest.mark.asyncio
async def test_artifact_upload_and_complete_preserve_frozen_status_codes() -> None:
    transport = httpx.ASGITransport(app=build_app())
    headers = {
        "Authorization": "Bearer mock",
        "X-Request-ID": "req-artifact-api",
        "Idempotency-Key": "artifact-idempotency-001",
    }
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        upload = await client.post(
            "/api/v1/artifacts/uploads",
            headers=headers,
            json={
                "name": "result.txt",
                "size": 12,
                "content_type": "text/plain",
                "content_hash": HASH,
            },
        )
        complete = await client.post(
            f"/api/v1/artifacts/{ARTIFACT_ID}/complete",
            headers=headers,
            json={"size": 12, "content_hash": HASH},
        )

    assert upload.status_code == 201
    assert upload.json()["artifact_id"] == str(ARTIFACT_ID)
    assert complete.status_code == 202
    assert complete.json()["status"] == "SCANNING"


@pytest.mark.asyncio
async def test_artifact_download_and_delete_preserve_frozen_contract() -> None:
    transport = httpx.ASGITransport(app=build_app())
    headers = {
        "Authorization": "Bearer mock",
        "X-Request-ID": "req-artifact-lifecycle-api",
        "Idempotency-Key": "artifact-delete-001",
    }
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        download = await client.get(
            f"/api/v1/artifacts/{ARTIFACT_ID}/download",
            headers=headers,
        )
        deleted = await client.delete(
            f"/api/v1/artifacts/{ARTIFACT_ID}",
            headers=headers,
        )

    assert download.status_code == 200
    assert download.json()["url"].startswith("https://objects.test/download")
    assert deleted.status_code == 202
    assert deleted.json()["status"] == "ACCEPTED"


@pytest.mark.asyncio
async def test_artifact_download_gateway_streams_with_private_security_headers() -> (
    None
):
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/api/v1/artifact-downloads/55555555-5555-4555-8555-555555555555",
            params={"token": "t" * 43},
        )

    assert response.status_code == 200
    assert response.content == b"artifact-body"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-length"] == "13"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "result.txt" in response.headers["content-disposition"]


@pytest.mark.asyncio
async def test_artifact_download_gateway_supports_single_byte_range() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/api/v1/artifact-downloads/55555555-5555-4555-8555-555555555555",
            params={"token": "t" * 43},
            headers={"Range": "bytes=0-7"},
        )

    assert response.status_code == 206
    assert response.content == b"artifact"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-range"] == "bytes 0-7/13"
    assert response.headers["content-length"] == "8"


@pytest.mark.asyncio
async def test_artifact_download_gateway_returns_416_with_total_size() -> None:
    class RejectingGateway:
        async def open_download(self, **kwargs: object) -> ArtifactTrustedContent:
            del kwargs
            from packages.contracts.public import range_not_satisfiable

            raise range_not_satisfiable(total_size=13)

    settings = AppSettings()
    app = create_app(
        settings,
        identity_provider=MockIdentityProvider(settings, now=lambda: NOW),
        artifact_service=cast(ArtifactManagementService, ArtifactServiceStub()),
        artifact_download_gateway=cast(
            ArtifactDownloadGatewayService, RejectingGateway()
        ),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/api/v1/artifact-downloads/55555555-5555-4555-8555-555555555555",
            params={"token": "t" * 43},
            headers={"Range": "bytes=13-"},
        )

    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */13"
    assert response.json()["error"]["code"] == "RANGE_NOT_SATISFIABLE"


@pytest.mark.asyncio
async def test_artifact_routes_fail_closed_when_service_is_not_configured() -> None:
    settings = AppSettings()
    app = create_app(
        settings,
        identity_provider=MockIdentityProvider(settings, now=lambda: NOW),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(
            f"/api/v1/artifacts/{ARTIFACT_ID}",
            headers={"Authorization": "Bearer mock"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"

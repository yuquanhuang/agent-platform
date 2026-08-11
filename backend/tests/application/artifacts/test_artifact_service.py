"""Artifact upload authorization and server-observed completion tests."""

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest

from packages.application.artifacts import (
    ArtifactDownloadGrant,
    ArtifactGrantUrlPolicy,
    ArtifactManagementService,
    ArtifactObjectObservation,
    ArtifactStore,
    ArtifactUploadGrant,
)
from packages.application.metadata import RequestMetadata
from packages.contracts.generated.core_models import (
    ArtifactCompleteRequest,
    ArtifactUploadCreateRequest,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    PlatformError,
    SubjectType,
    TenantContext,
)
from packages.domain.public import (
    ArtifactRecord,
    ArtifactStatus,
    MutationOutcome,
    OperationRecord,
    TenantAccess,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
ARTIFACT_ID = UUID("33333333-3333-4333-8333-333333333333")
OPERATION_ID = UUID("44444444-4444-4444-8444-444444444444")
NOW = datetime.now(UTC)
HASH = "sha256:" + "a" * 64


class ArtifactStub:
    def __init__(self) -> None:
        self.permissions = frozenset(
            {
                "artifact:create",
                "artifact:read",
                "artifact:download",
                "artifact:delete",
                "run:read",
            }
        )
        self.record = _record()
        self.observation = ArtifactObjectObservation(
            size_bytes=12,
            content_hash=HASH,
            content_type="text/plain",
        )
        self.grant = ArtifactUploadGrant(
            upload_url="https://objects.test/upload?signature=opaque",
            expires_at=NOW + timedelta(minutes=5),
            required_headers={"Content-Type": "text/plain"},
        )
        self.failed_codes: list[str] = []
        self.marked_scanning = False
        self.download_calls = 0
        self.download_confirmed = True

    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess:
        return TenantAccess(
            context=TenantContext(
                tenant_id=str(TENANT_ID),
                subject_type=SubjectType.USER,
                subject_id=str(ACTOR_ID),
                membership_version=1,
                auth_time=NOW,
                request_id=metadata.request_id,
                trace_id=metadata.trace_id,
            ),
            permissions=self.permissions,
        )

    async def create_upload(self, context: TenantContext, **kwargs: object):
        return self.record

    async def get_owned(self, context: TenantContext, **kwargs: object):
        return self.record

    async def mark_scanning(self, context: TenantContext, **kwargs: object):
        self.marked_scanning = True
        self.record = _record(status="SCANNING")
        return self.record

    async def fail_upload(self, context: TenantContext, **kwargs: object) -> None:
        self.failed_codes.append(cast(str, kwargs["code"]))

    async def get_downloadable(self, context: TenantContext, **kwargs: object):
        return self.record

    async def request_delete(self, context: TenantContext, **kwargs: object):
        return MutationOutcome(
            value=OperationRecord(
                id=OPERATION_ID,
                operation_type="artifact.delete",
                status="ACCEPTED",
                resource_type="artifact",
                resource_id=ARTIFACT_ID,
                result=None,
                error=None,
                created_at=NOW,
                updated_at=NOW,
                finished_at=None,
            )
        )

    async def confirm_download(self, context: TenantContext, **kwargs: object) -> bool:
        return self.download_confirmed

    async def create_upload_grant(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> ArtifactUploadGrant:
        return self.grant

    async def inspect_quarantine(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> ArtifactObjectObservation:
        return self.observation

    async def create_download_grant(
        self,
        context: TenantContext,
        *,
        artifact: ArtifactRecord,
        expires_at: datetime,
    ) -> ArtifactDownloadGrant:
        self.download_calls += 1
        return ArtifactDownloadGrant(
            artifact_id=artifact.id,
            url="https://objects.test/download?signature=opaque",
            expires_at=expires_at,
        )


def _record(
    *,
    status: str = "UPLOADING",
    run_id: UUID | None = None,
    expires_at: datetime | None = None,
) -> ArtifactRecord:
    return ArtifactRecord(
        id=ARTIFACT_ID,
        tenant_id=TENANT_ID,
        workspace_id=None,
        run_id=run_id,
        owner_user_id=ACTOR_ID,
        name="result.txt",
        quarantine_object_uri=(
            f"quarantine://tenant/{TENANT_ID}/artifact/{ARTIFACT_ID}/source"
        ),
        object_uri=(
            f"artifact://tenant/{TENANT_ID}/artifact/{ARTIFACT_ID}"
            if status in {"AVAILABLE", "EXPIRED", "DELETING", "DELETED"}
            else None
        ),
        content_hash=HASH,
        size_bytes=12,
        content_type="text/plain",
        status=cast(ArtifactStatus, status),
        required_output=False,
        scan_result=None,
        upload_expires_at=NOW + timedelta(minutes=15),
        created_at=NOW,
        updated_at=NOW,
        expires_at=expires_at or NOW + timedelta(days=30),
        deleted_at=None,
    )


def _service(stub: ArtifactStub) -> ArtifactManagementService:
    return ArtifactManagementService(
        stub,
        cast(ArtifactStore, stub),
        stub,
        ArtifactGrantUrlPolicy(frozenset({"https://objects.test"})),
    )


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        identity_issuer="https://issuer.test",
        external_subject="artifact-user",
        display_name="Artifact User",
        active_tenant_id=str(TENANT_ID),
        membership_version=1,
        auth_time=NOW,
    )


def _metadata() -> RequestMetadata:
    return RequestMetadata(request_id="req-artifact", trace_id="trace-artifact")


def _create_request(**changes: object) -> ArtifactUploadCreateRequest:
    values: dict[str, object] = {
        "name": "result.txt",
        "size": 12,
        "content_type": "text/plain",
        "content_hash": HASH,
    }
    values.update(changes)
    return ArtifactUploadCreateRequest.model_validate(values)


@pytest.mark.asyncio
async def test_create_upload_returns_restricted_object_store_grant() -> None:
    stub = ArtifactStub()

    accepted = await _service(stub).create_upload(
        _principal(),
        request=_create_request(),
        idempotency_key="artifact-create-001",
        metadata=_metadata(),
    )

    assert accepted.artifact_id == str(ARTIFACT_ID)
    assert accepted.required_headers == {"Content-Type": "text/plain"}


@pytest.mark.asyncio
async def test_create_upload_rejects_path_like_filename_before_persistence() -> None:
    stub = ArtifactStub()

    with pytest.raises(PlatformError) as error:
        await _service(stub).create_upload(
            _principal(),
            request=_create_request(name="../secret.txt"),
            idempotency_key="artifact-create-002",
            metadata=_metadata(),
        )

    assert error.value.code == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_create_upload_rejects_sensitive_required_header() -> None:
    stub = ArtifactStub()
    stub.grant = ArtifactUploadGrant(
        upload_url="https://objects.test/upload",
        expires_at=NOW + timedelta(minutes=5),
        required_headers={"Authorization": "secret"},
    )

    with pytest.raises(RuntimeError, match="headers are unsafe"):
        await _service(stub).create_upload(
            _principal(),
            request=_create_request(),
            idempotency_key="artifact-create-003",
            metadata=_metadata(),
        )


@pytest.mark.asyncio
async def test_complete_upload_marks_scanning_only_after_server_observation() -> None:
    stub = ArtifactStub()

    artifact = await _service(stub).complete_upload(
        _principal(),
        artifact_id=str(ARTIFACT_ID),
        request=ArtifactCompleteRequest(size=12, content_hash=HASH),
        idempotency_key="artifact-complete-001",
        metadata=_metadata(),
    )

    assert artifact.status == "SCANNING"
    assert stub.marked_scanning is True


@pytest.mark.asyncio
async def test_complete_upload_fails_artifact_on_server_observed_mismatch() -> None:
    stub = ArtifactStub()
    stub.observation = ArtifactObjectObservation(
        size_bytes=11,
        content_hash=HASH,
        content_type="text/plain",
    )

    with pytest.raises(PlatformError) as error:
        await _service(stub).complete_upload(
            _principal(),
            artifact_id=str(ARTIFACT_ID),
            request=ArtifactCompleteRequest(size=12, content_hash=HASH),
            idempotency_key="artifact-complete-002",
            metadata=_metadata(),
        )

    assert error.value.code == "ARTIFACT_UPLOAD_MISMATCH"
    assert stub.failed_codes == ["ARTIFACT_UPLOAD_MISMATCH"]
    assert stub.marked_scanning is False


@pytest.mark.asyncio
async def test_get_artifact_requires_read_permission() -> None:
    stub = ArtifactStub()
    stub.permissions = frozenset({"artifact:create"})

    with pytest.raises(PlatformError) as error:
        await _service(stub).get_artifact(
            _principal(), artifact_id=str(ARTIFACT_ID), metadata=_metadata()
        )

    assert error.value.code == "PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_download_returns_short_lived_single_artifact_grant() -> None:
    stub = ArtifactStub()
    stub.record = _record(status="AVAILABLE")

    result = await _service(stub).create_download(
        _principal(), artifact_id=str(ARTIFACT_ID), metadata=_metadata()
    )

    assert result.url.startswith("https://objects.test/download")
    assert result.expires_at <= NOW + timedelta(minutes=6)
    assert stub.download_calls == 1


@pytest.mark.asyncio
async def test_download_rejects_expired_artifact_without_object_grant() -> None:
    stub = ArtifactStub()
    stub.record = _record(status="EXPIRED")

    with pytest.raises(PlatformError) as error:
        await _service(stub).create_download(
            _principal(), artifact_id=str(ARTIFACT_ID), metadata=_metadata()
        )

    assert error.value.code == "ARTIFACT_EXPIRED"
    assert stub.download_calls == 0


@pytest.mark.asyncio
async def test_download_fails_closed_when_deletion_wins_authorization_race() -> None:
    stub = ArtifactStub()
    stub.record = _record(status="AVAILABLE")
    stub.download_confirmed = False

    with pytest.raises(PlatformError) as error:
        await _service(stub).create_download(
            _principal(), artifact_id=str(ARTIFACT_ID), metadata=_metadata()
        )

    assert error.value.code == "RESOURCE_STATE_CONFLICT"
    assert stub.download_calls == 1


@pytest.mark.asyncio
async def test_download_rejects_grant_bound_to_another_artifact() -> None:
    stub = ArtifactStub()
    stub.record = _record(status="AVAILABLE")

    async def wrong_grant(
        context: TenantContext,
        *,
        artifact: ArtifactRecord,
        expires_at: datetime,
    ) -> ArtifactDownloadGrant:
        return ArtifactDownloadGrant(
            artifact_id=UUID(int=99),
            url="https://objects.test/download?signature=opaque",
            expires_at=expires_at,
        )

    stub.create_download_grant = wrong_grant  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="unsafe download URL"):
        await _service(stub).create_download(
            _principal(), artifact_id=str(ARTIFACT_ID), metadata=_metadata()
        )


@pytest.mark.asyncio
async def test_run_artifact_download_requires_current_run_read_permission() -> None:
    stub = ArtifactStub()
    stub.record = _record(status="AVAILABLE", run_id=UUID(int=9))
    stub.permissions = frozenset({"artifact:download"})

    with pytest.raises(PlatformError) as error:
        await _service(stub).create_download(
            _principal(), artifact_id=str(ARTIFACT_ID), metadata=_metadata()
        )

    assert error.value.code == "PERMISSION_DENIED"
    assert stub.download_calls == 0


@pytest.mark.asyncio
async def test_delete_returns_queryable_operation() -> None:
    stub = ArtifactStub()
    stub.record = _record(status="AVAILABLE")

    accepted = await _service(stub).delete_artifact(
        _principal(),
        artifact_id=str(ARTIFACT_ID),
        idempotency_key="artifact-delete-001",
        metadata=_metadata(),
    )

    assert accepted.operation_id == str(OPERATION_ID)
    assert accepted.status_url == f"/api/v1/operations/{OPERATION_ID}"

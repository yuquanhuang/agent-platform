"""Artifact API-to-object-store isolation and cleanup E2E with a controlled adapter."""

from __future__ import annotations

import hashlib
import io
import secrets
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import BinaryIO, Literal, cast
from urllib.parse import parse_qs, urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
import pytest
from pydantic import JsonValue, SecretStr

from apps.api.app import create_app
from packages.application.artifacts import (
    ARTIFACT_DELETE_REQUESTED_EVENT,
    ARTIFACT_SCAN_REQUESTED_EVENT,
    ArchiveAwareArtifactSecurityScanner,
    ArtifactByteRange,
    ArtifactDeleteProcessor,
    ArtifactDownloadCredential,
    ArtifactDownloadGatewayService,
    ArtifactDownloadGrantRecord,
    ArtifactGrantUrlPolicy,
    ArtifactManagementService,
    ArtifactObjectObservation,
    ArtifactScanProcessor,
    ArtifactScanVerdict,
    ArtifactTrustedContent,
    ArtifactUploadGrant,
    RetryableArtifactDeleteError,
)
from packages.application.metadata import RequestMetadata
from packages.contracts.generated.core_models import ArtifactUploadCreateRequest
from packages.contracts.public import (
    AuthenticatedPrincipal,
    IdentityProvider,
    SubjectType,
    TenantContext,
)
from packages.domain.public import (
    ArtifactRecord,
    ArtifactStatus,
    MutationOutcome,
    OperationRecord,
    OutboxEvent,
    OutboxStatus,
    TenantAccess,
    artifact_uri,
)
from packages.infrastructure.public import AppSettings

TENANT_A = UUID("11111111-1111-4111-8111-111111111111")
TENANT_B = UUID("22222222-2222-4222-8222-222222222222")
USER_A = UUID("33333333-3333-4333-8333-333333333333")
USER_B = UUID("44444444-4444-4444-8444-444444444444")
USER_C = UUID("55555555-5555-4555-8555-555555555555")
RUN_A = UUID("66666666-6666-4666-8666-666666666666")


class ControlledIdentityProvider(IdentityProvider):
    def authenticate(self, authorization: str | None) -> AuthenticatedPrincipal:
        token = (authorization or "").removeprefix("Bearer ")
        if token not in {"a", "a-no-run", "b", "c"}:
            raise ValueError("unknown controlled identity")
        tenant_id = TENANT_B if token == "b" else TENANT_A
        return AuthenticatedPrincipal(
            identity_issuer="https://issuer.test",
            external_subject=token,
            display_name=f"Controlled {token}",
            active_tenant_id=str(tenant_id),
            membership_version=1,
            auth_time=datetime.now(UTC),
        )


class ControlledArtifactPlatform:
    """In-memory adapter with tenant-bound object keys and revocable grants."""

    def __init__(self) -> None:
        self.records: dict[tuple[UUID, UUID], ArtifactRecord] = {}
        self.operations: dict[UUID, OperationRecord] = {}
        self.events: list[OutboxEvent] = []
        self.quarantine: dict[tuple[UUID, UUID], bytes] = {}
        self.trusted: dict[tuple[UUID, UUID], bytes] = {}
        self.upload_tokens: dict[str, tuple[UUID, UUID, datetime]] = {}
        self.download_grants: dict[UUID, ArtifactDownloadGrantRecord] = {}
        self.revoked: set[tuple[UUID, UUID]] = set()
        self.delete_failures = 0
        self.source_run_visible = True
        self.audit: list[dict[str, str]] = []

    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess:
        actor = {
            "a": USER_A,
            "a-no-run": USER_A,
            "b": USER_B,
            "c": USER_C,
        }[principal.external_subject]
        permissions = {
            "artifact:create",
            "artifact:read",
            "artifact:download",
            "artifact:delete",
        }
        if principal.external_subject != "a-no-run":
            permissions.add("run:read")
        return TenantAccess(
            context=TenantContext(
                tenant_id=cast(str, principal.active_tenant_id),
                subject_type=SubjectType.USER,
                subject_id=str(actor),
                membership_version=1,
                auth_time=principal.auth_time,
                request_id=metadata.request_id,
                trace_id=metadata.trace_id,
            ),
            permissions=frozenset(permissions),
        )

    async def create_upload(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        owner_user_id: UUID,
        request: ArtifactUploadCreateRequest,
        quarantine_object_uri: str,
        upload_expires_at: datetime,
        expires_at: datetime,
        **kwargs: object,
    ) -> ArtifactRecord:
        key = (UUID(context.tenant_id), artifact_id)
        existing = self.records.get(key)
        if existing is not None:
            return existing
        now = datetime.now(UTC)
        record = ArtifactRecord(
            id=artifact_id,
            tenant_id=key[0],
            workspace_id=None,
            run_id=None,
            owner_user_id=owner_user_id,
            name=request.name,
            quarantine_object_uri=quarantine_object_uri,
            object_uri=None,
            content_hash=request.content_hash,
            size_bytes=request.size,
            content_type=request.content_type,
            status=cast(ArtifactStatus, "UPLOADING"),
            required_output=False,
            scan_result=None,
            upload_expires_at=upload_expires_at,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
            retention_delete_after=None,
            deleted_at=None,
        )
        self.records[key] = record
        return record

    async def get_owned(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        owner_user_id: UUID,
    ) -> ArtifactRecord | None:
        record = self.records.get((UUID(context.tenant_id), artifact_id))
        if record is None or record.owner_user_id != owner_user_id:
            return None
        return record

    async def mark_scanning(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        owner_user_id: UUID,
        now: datetime,
        **kwargs: object,
    ) -> ArtifactRecord | None:
        record = await self.get_owned(
            context, artifact_id=artifact_id, owner_user_id=owner_user_id
        )
        if record is None:
            return None
        if record.status == "UPLOADING":
            record = replace(record, status="SCANNING", updated_at=now)
            self.records[(record.tenant_id, record.id)] = record
            self.events.append(_event(record, ARTIFACT_SCAN_REQUESTED_EVENT))
        return record

    async def fail_upload(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        owner_user_id: UUID,
        code: str,
        now: datetime,
    ) -> None:
        record = await self.get_owned(
            context, artifact_id=artifact_id, owner_user_id=owner_user_id
        )
        if record is not None:
            self.records[(record.tenant_id, record.id)] = replace(
                record, status="FAILED", updated_at=now
            )

    async def get_downloadable(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        owner_user_id: UUID,
        **kwargs: object,
    ) -> ArtifactRecord | None:
        record = await self.get_owned(
            context, artifact_id=artifact_id, owner_user_id=owner_user_id
        )
        if (
            record is not None
            and record.run_id is not None
            and not self.source_run_visible
        ):
            return None
        return record

    def issue(self) -> ArtifactDownloadCredential:
        token = secrets.token_urlsafe(32)
        return ArtifactDownloadCredential(
            grant_id=uuid5(NAMESPACE_URL, f"artifact-grant/{token}"),
            token=SecretStr(token),
            token_hash="sha256:" + hashlib.sha256(token.encode()).hexdigest(),
        )

    async def create_download_grant(
        self,
        context: TenantContext,
        *,
        grant_id: UUID,
        artifact_id: UUID,
        owner_user_id: UUID,
        token_hash: str,
        grant_expires_at: datetime,
        **kwargs: object,
    ) -> bool:
        record = await self.get_owned(
            context, artifact_id=artifact_id, owner_user_id=owner_user_id
        )
        if record is None or record.status != "AVAILABLE":
            return False
        self.download_grants[grant_id] = ArtifactDownloadGrantRecord(
            id=grant_id,
            tenant_id=record.tenant_id,
            artifact_id=record.id,
            owner_user_id=record.owner_user_id,
            token_hash=token_hash,
            expires_at=grant_expires_at,
            revoked_at=None,
            artifact=record,
        )
        self.audit.append(
            {"action": "artifact.download", "artifact_id": str(record.id)}
        )
        return True

    async def resolve_download_grant(
        self,
        *,
        grant_id: UUID,
        token_hash: str,
        service_subject_id: UUID,
        metadata: RequestMetadata,
        now: datetime,
    ) -> ArtifactDownloadGrantRecord | None:
        grant = self.download_grants.get(grant_id)
        if (
            grant is None
            or grant.token_hash != token_hash
            or grant.revoked_at is not None
            or grant.expires_at <= now
        ):
            return None
        record = self.records.get((grant.tenant_id, grant.artifact_id))
        if record is None or record.status != "AVAILABLE":
            return None
        return replace(grant, artifact=record)

    async def record_download_open(
        self,
        context: TenantContext,
        *,
        grant_id: UUID,
        artifact_id: UUID,
        **kwargs: object,
    ) -> bool:
        grant = self.download_grants.get(grant_id)
        if (
            grant is None
            or grant.artifact_id != artifact_id
            or grant.revoked_at is not None
        ):
            return False
        self.audit.append(
            {"action": "artifact.download.open", "artifact_id": str(artifact_id)}
        )
        return True

    async def is_download_grant_active(
        self,
        context: TenantContext,
        *,
        grant_id: UUID,
        artifact_id: UUID,
        now: datetime,
    ) -> bool:
        grant = self.download_grants.get(grant_id)
        artifact = self.records.get((UUID(context.tenant_id), artifact_id))
        return bool(
            grant is not None
            and artifact is not None
            and str(grant.tenant_id) == context.tenant_id
            and grant.artifact_id == artifact_id
            and grant.revoked_at is None
            and grant.expires_at > now
            and artifact.status == "AVAILABLE"
            and artifact.expires_at > now
        )

    async def request_delete(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        owner_user_id: UUID,
        now: datetime,
        **kwargs: object,
    ) -> MutationOutcome[OperationRecord] | None:
        record = await self.get_owned(
            context, artifact_id=artifact_id, owner_user_id=owner_user_id
        )
        if record is None:
            return None
        operation_id = uuid5(
            NAMESPACE_URL, f"artifact-delete/{record.tenant_id}/{record.id}"
        )
        operation = self.operations.get(operation_id)
        if operation is None:
            operation = OperationRecord(
                id=operation_id,
                operation_type="artifact.delete",
                status="ACCEPTED",
                resource_type="artifact",
                resource_id=record.id,
                result=None,
                error=None,
                created_at=now,
                updated_at=now,
                finished_at=None,
            )
            self.operations[operation_id] = operation
            self.records[(record.tenant_id, record.id)] = replace(
                record, status="DELETING", updated_at=now
            )
            self.events.append(
                _event(
                    record,
                    ARTIFACT_DELETE_REQUESTED_EVENT,
                    operation_id=operation_id,
                )
            )
            for grant_id, grant in tuple(self.download_grants.items()):
                if (
                    grant.tenant_id == record.tenant_id
                    and grant.artifact_id == record.id
                ):
                    self.download_grants[grant_id] = replace(grant, revoked_at=now)
        return MutationOutcome(value=operation)

    async def get_for_scan(
        self, context: TenantContext, *, artifact_id: UUID
    ) -> ArtifactRecord | None:
        return self.records.get((UUID(context.tenant_id), artifact_id))

    async def complete_scan(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        status: Literal["AVAILABLE", "REJECTED"],
        object_uri: str | None,
        scan_result: dict[str, JsonValue],
        now: datetime,
    ) -> ArtifactRecord:
        key = (UUID(context.tenant_id), artifact_id)
        record = self.records[key]
        updated = replace(
            record,
            status=status,
            object_uri=object_uri,
            scan_result=scan_result,
            updated_at=now,
        )
        self.records[key] = updated
        return updated

    async def fail_scan(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        code: str,
        now: datetime,
    ) -> None:
        key = (UUID(context.tenant_id), artifact_id)
        self.records[key] = replace(self.records[key], status="FAILED", updated_at=now)

    async def expire_due(
        self, context: TenantContext, *, now: datetime, limit: int
    ) -> int:
        return 0

    async def reclaim_expired_uploads(
        self, context: TenantContext, *, now: datetime, limit: int
    ) -> int:
        return 0

    async def purge_retention_due(
        self, context: TenantContext, *, now: datetime, limit: int
    ) -> int:
        return 0

    async def recover_failed_deletes(
        self, context: TenantContext, *, now: datetime, limit: int
    ) -> int:
        return 0

    async def get_for_delete(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        operation_id: UUID,
    ) -> ArtifactRecord | None:
        operation = self.operations.get(operation_id)
        if operation is None or operation.resource_id != artifact_id:
            return None
        return self.records.get((UUID(context.tenant_id), artifact_id))

    async def complete_delete(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        operation_id: UUID,
        now: datetime,
    ) -> None:
        key = (UUID(context.tenant_id), artifact_id)
        record = self.records[key]
        self.records[key] = replace(
            record, status="DELETED", object_uri=None, updated_at=now, deleted_at=now
        )
        operation = self.operations[operation_id]
        self.operations[operation_id] = replace(
            operation,
            status="SUCCEEDED",
            updated_at=now,
            finished_at=now,
        )

    async def fail_delete(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        operation_id: UUID,
        code: str,
        now: datetime,
    ) -> None:
        operation = self.operations[operation_id]
        self.operations[operation_id] = replace(
            operation,
            status="FAILED",
            error={"code": code},
            updated_at=now,
            finished_at=now,
        )

    async def create_upload_grant(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> ArtifactUploadGrant:
        token = uuid5(NAMESPACE_URL, f"upload/{artifact.tenant_id}/{artifact.id}").hex
        expires_at = min(
            artifact.upload_expires_at, datetime.now(UTC) + timedelta(minutes=5)
        )
        self.upload_tokens[token] = (artifact.tenant_id, artifact.id, expires_at)
        return ArtifactUploadGrant(
            upload_url=f"https://objects.test/upload?token={token}",
            expires_at=expires_at,
            required_headers={"Content-Type": artifact.content_type},
        )

    def put_upload(self, url: str, content: bytes) -> None:
        token = _token(url)
        tenant_id, artifact_id, expires_at = self.upload_tokens[token]
        assert expires_at > datetime.now(UTC)
        self.quarantine[(tenant_id, artifact_id)] = content

    async def inspect_quarantine(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> ArtifactObjectObservation:
        content = self.quarantine[(UUID(context.tenant_id), artifact.id)]
        return ArtifactObjectObservation(
            size_bytes=len(content),
            content_hash="sha256:" + hashlib.sha256(content).hexdigest(),
            content_type=artifact.content_type,
        )

    async def copy_quarantine(
        self,
        context: TenantContext,
        *,
        artifact: ArtifactRecord,
        destination: BinaryIO,
        max_bytes: int,
    ) -> int:
        content = self.quarantine[(UUID(context.tenant_id), artifact.id)]
        if len(content) > max_bytes:
            raise ValueError("controlled quarantine object exceeds the read bound")
        destination.write(content)
        return len(content)

    async def scan(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> ArtifactScanVerdict:
        return ArtifactScanVerdict(
            decision="PASSED",
            engine="controlled-malware-scanner",
            definition_version="1",
            findings=(),
            scanned_at=datetime.now(UTC),
        )

    async def promote(self, context: TenantContext, *, artifact: ArtifactRecord) -> str:
        key = (UUID(context.tenant_id), artifact.id)
        self.trusted[key] = self.quarantine[key]
        return artifact_uri(tenant_id=artifact.tenant_id, artifact_id=artifact.id)

    async def open_trusted_artifact(
        self,
        context: TenantContext,
        *,
        artifact: ArtifactRecord,
        byte_range: ArtifactByteRange | None,
    ) -> ArtifactTrustedContent:
        content = self.trusted[(UUID(context.tenant_id), artifact.id)]
        selected = (
            content
            if byte_range is None
            else content[byte_range.start : byte_range.end_inclusive + 1]
        )

        async def body():
            yield selected

        return ArtifactTrustedContent(
            body=body(),
            size_bytes=len(selected),
            total_size_bytes=len(content),
            content_type=artifact.content_type,
            name=artifact.name,
            byte_range=byte_range,
        )

    async def revoke_download_access(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> None:
        self.revoked.add((UUID(context.tenant_id), artifact.id))

    async def delete_artifact_objects(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> None:
        if self.delete_failures > 0:
            self.delete_failures -= 1
            raise RetryableArtifactDeleteError("controlled object store unavailable")
        key = (UUID(context.tenant_id), artifact.id)
        self.quarantine.pop(key, None)
        self.trusted.pop(key, None)

    def bind_run(self, tenant_id: UUID, artifact_id: UUID) -> None:
        key = (tenant_id, artifact_id)
        self.records[key] = replace(self.records[key], run_id=RUN_A)


@pytest.mark.asyncio
async def test_artifact_isolation_archive_rejection_and_revocable_cleanup_e2e() -> None:
    platform = ControlledArtifactPlatform()
    service = ArtifactManagementService(
        platform,
        platform,
        platform,
        ArtifactGrantUrlPolicy(frozenset({"https://objects.test"})),
        platform,
        "https://objects.test",
    )
    app = create_app(
        AppSettings(),
        identity_provider=ControlledIdentityProvider(),
        artifact_service=service,
        artifact_download_gateway=ArtifactDownloadGatewayService(
            platform,
            platform,
            service_subject_id=USER_A,
        ),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        safe_content = b"tenant-a-safe-artifact"
        upload = await _create_and_upload(
            client, platform, "a", safe_content, "shared.txt"
        )
        artifact_id = UUID(cast(str, upload["artifact_id"]))
        complete = await client.post(
            f"/api/v1/artifacts/{artifact_id}/complete",
            headers=_headers("a", "complete-safe"),
            json={"size": len(safe_content), "content_hash": _hash(safe_content)},
        )
        assert complete.status_code == 202
        platform.bind_run(TENANT_A, artifact_id)
        scan_event = _latest_event(platform, ARTIFACT_SCAN_REQUESTED_EVENT, artifact_id)
        await ArtifactScanProcessor(
            platform,
            ArchiveAwareArtifactSecurityScanner(platform, platform),
            platform,
        ).process(_service_context(TENANT_A, USER_A), scan_event)

        metadata = await client.get(
            f"/api/v1/artifacts/{artifact_id}", headers=_headers("a")
        )
        assert metadata.status_code == 200
        assert metadata.json()["status"] == "AVAILABLE"

        download = await client.get(
            f"/api/v1/artifacts/{artifact_id}/download", headers=_headers("a")
        )
        assert download.status_code == 200
        download_url = download.json()["url"]
        downloaded = await client.get(download_url)
        assert downloaded.status_code == 200
        assert downloaded.content == safe_content
        invalid_token = await client.get(download_url.replace("token=", "token=x"))
        assert invalid_token.status_code == 404

        for token in ("b", "c"):
            hidden = await client.get(
                f"/api/v1/artifacts/{artifact_id}", headers=_headers(token)
            )
            assert hidden.status_code == 404
        no_run_access = await client.get(
            f"/api/v1/artifacts/{artifact_id}/download",
            headers=_headers("a-no-run"),
        )
        assert no_run_access.status_code == 403
        platform.source_run_visible = False
        deleted_source = await client.get(
            f"/api/v1/artifacts/{artifact_id}/download", headers=_headers("a")
        )
        assert deleted_source.status_code == 404
        platform.source_run_visible = True

        tenant_b_upload = await _create_and_upload(
            client, platform, "b", b"tenant-b", "shared.txt"
        )
        assert tenant_b_upload["artifact_id"] != str(artifact_id)

        hostile_content = _hostile_zip()
        hostile_upload = await _create_and_upload(
            client,
            platform,
            "a",
            hostile_content,
            "hostile.zip",
            content_type="application/zip",
        )
        hostile_id = UUID(cast(str, hostile_upload["artifact_id"]))
        hostile_complete = await client.post(
            f"/api/v1/artifacts/{hostile_id}/complete",
            headers=_headers("a", "complete-hostile"),
            json={"size": len(hostile_content), "content_hash": _hash(hostile_content)},
        )
        assert hostile_complete.status_code == 202
        hostile_event = _latest_event(
            platform, ARTIFACT_SCAN_REQUESTED_EVENT, hostile_id
        )
        await ArtifactScanProcessor(
            platform,
            ArchiveAwareArtifactSecurityScanner(platform, platform),
            platform,
        ).process(_service_context(TENANT_A, USER_A), hostile_event)
        assert platform.records[(TENANT_A, hostile_id)].status == "REJECTED"
        assert (TENANT_A, hostile_id) not in platform.trusted

        deletion = await client.delete(
            f"/api/v1/artifacts/{artifact_id}",
            headers=_headers("a", "delete-safe"),
        )
        assert deletion.status_code == 202
        delete_event = _latest_event(
            platform, ARTIFACT_DELETE_REQUESTED_EVENT, artifact_id
        )
        platform.delete_failures = 1
        delete_processor = ArtifactDeleteProcessor(platform, platform)
        with pytest.raises(RetryableArtifactDeleteError):
            await delete_processor.process(
                _service_context(TENANT_A, USER_A),
                delete_event,
                now=datetime.now(UTC),
            )
        assert platform.records[(TENANT_A, artifact_id)].status == "DELETING"
        revoked_download = await client.get(download_url)
        assert revoked_download.status_code == 404

        await delete_processor.process(
            _service_context(TENANT_A, USER_A),
            delete_event,
            now=datetime.now(UTC),
        )
        assert platform.records[(TENANT_A, artifact_id)].status == "DELETED"
        assert (TENANT_A, artifact_id) not in platform.quarantine
        assert (TENANT_A, artifact_id) not in platform.trusted
        assert all("url" not in item and "token" not in item for item in platform.audit)


async def _create_and_upload(
    client: httpx.AsyncClient,
    platform: ControlledArtifactPlatform,
    token: str,
    content: bytes,
    name: str,
    *,
    content_type: str = "text/plain",
) -> dict[str, object]:
    response = await client.post(
        "/api/v1/artifacts/uploads",
        headers=_headers(token, f"create-{token}-{name}"),
        json={
            "name": name,
            "size": len(content),
            "content_type": content_type,
            "content_hash": _hash(content),
        },
    )
    assert response.status_code == 201
    body = response.json()
    platform.put_upload(cast(str, body["upload_url"]), content)
    return cast(dict[str, object], body)


def _headers(token: str, idempotency_key: str = "unused") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": f"req-{token}-{idempotency_key}",
        "Idempotency-Key": idempotency_key,
    }


def _hash(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _token(url: str) -> str:
    return parse_qs(urlsplit(url).query)["token"][0]


def _hostile_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("../secret.txt", b"unsafe")
    return output.getvalue()


def _event(
    artifact: ArtifactRecord,
    event_type: str,
    *,
    operation_id: UUID | None = None,
) -> OutboxEvent:
    payload = {"artifact_id": str(artifact.id)}
    if operation_id is not None:
        payload["operation_id"] = str(operation_id)
    return OutboxEvent(
        id=uuid5(NAMESPACE_URL, f"{event_type}/{artifact.tenant_id}/{artifact.id}"),
        tenant_id=artifact.tenant_id,
        aggregate_type="artifact",
        aggregate_id=artifact.id,
        event_type=event_type,
        payload=payload,
        payload_schema_version=1,
        status=OutboxStatus.PUBLISHING,
        attempts=1,
        next_attempt_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )


def _latest_event(
    platform: ControlledArtifactPlatform, event_type: str, artifact_id: UUID
) -> OutboxEvent:
    return next(
        event
        for event in reversed(platform.events)
        if event.event_type == event_type and event.aggregate_id == artifact_id
    )


def _service_context(tenant_id: UUID, actor_id: UUID) -> TenantContext:
    return TenantContext(
        tenant_id=str(tenant_id),
        subject_type=SubjectType.SERVICE,
        subject_id=str(actor_id),
        auth_time=datetime.now(UTC),
        request_id="req-controlled-artifact-worker",
        trace_id="trace-controlled-artifact-worker",
    )

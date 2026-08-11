"""User-owned Artifact upload authorization and completion use cases."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from packages.application.artifacts.url_security import ArtifactGrantUrlPolicy
from packages.application.metadata import RequestMetadata
from packages.application.resources.hashing import canonical_request_hash
from packages.contracts.generated.core_models import (
    Artifact,
    ArtifactCompleteRequest,
    ArtifactDownload,
    ArtifactUploadAccepted,
    ArtifactUploadCreateRequest,
    OperationAccepted,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    PlatformError,
    TenantContext,
    dependency_unavailable,
    permission_denied,
    resource_not_found,
    resource_state_conflict,
    validation_error,
)
from packages.domain.public import (
    ArtifactRecord,
    MutationOutcome,
    OperationRecord,
    TenantAccess,
    artifact_uri,
)

ARTIFACT_SCAN_REQUESTED_EVENT = "artifact.scan_requested.v1"
ARTIFACT_DELETE_REQUESTED_EVENT = "artifact.delete_requested.v1"
ARTIFACT_UPLOAD_TTL = timedelta(minutes=15)
ARTIFACT_DOWNLOAD_TTL = timedelta(minutes=5)
ARTIFACT_RETENTION = timedelta(days=30)
_CONTENT_TYPE_PATTERN = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
_HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_FORBIDDEN_UPLOAD_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "host",
        "proxy-authorization",
        "proxy-connection",
        "set-cookie",
    }
)


@dataclass(frozen=True, slots=True)
class ArtifactUploadGrant:
    upload_url: str
    expires_at: datetime
    required_headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class ArtifactDownloadGrant:
    artifact_id: UUID
    url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ArtifactObjectObservation:
    size_bytes: int
    content_hash: str
    content_type: str


class ArtifactObjectNotFound(RuntimeError):
    """The expected quarantine object does not exist."""


class ArtifactObjectStoreUnavailable(RuntimeError):
    """The trusted object store cannot complete the requested operation."""


class ArtifactObjectStore(Protocol):
    async def create_upload_grant(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> ArtifactUploadGrant: ...

    async def inspect_quarantine(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> ArtifactObjectObservation: ...

    async def create_download_grant(
        self,
        context: TenantContext,
        *,
        artifact: ArtifactRecord,
        expires_at: datetime,
    ) -> ArtifactDownloadGrant: ...


class ArtifactAccessResolver(Protocol):
    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess: ...


class ArtifactStore(Protocol):
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
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> ArtifactRecord: ...

    async def get_owned(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        owner_user_id: UUID,
    ) -> ArtifactRecord | None: ...

    async def mark_scanning(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        owner_user_id: UUID,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
        now: datetime,
    ) -> ArtifactRecord | None: ...

    async def fail_upload(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        owner_user_id: UUID,
        code: str,
        now: datetime,
    ) -> None: ...

    async def get_downloadable(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        owner_user_id: UUID,
        metadata: RequestMetadata,
        now: datetime,
    ) -> ArtifactRecord | None: ...

    async def request_delete(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        owner_user_id: UUID,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
        now: datetime,
    ) -> MutationOutcome[OperationRecord] | None: ...

    async def confirm_download(
        self,
        context: TenantContext,
        *,
        artifact_id: UUID,
        owner_user_id: UUID,
        grant_expires_at: datetime,
        metadata: RequestMetadata,
        now: datetime,
    ) -> bool: ...


class ArtifactManagementService:
    """Authorize Artifact metadata while keeping object locations server-only."""

    def __init__(
        self,
        access_resolver: ArtifactAccessResolver,
        store: ArtifactStore,
        object_store: ArtifactObjectStore,
        url_policy: ArtifactGrantUrlPolicy,
    ) -> None:
        self._access_resolver = access_resolver
        self._store = store
        self._objects = object_store
        self._url_policy = url_policy

    async def create_upload(
        self,
        principal: AuthenticatedPrincipal,
        *,
        request: ArtifactUploadCreateRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> ArtifactUploadAccepted:
        _validate_upload_request(request)
        access = await self._access(principal, "create", metadata)
        owner_user_id = _actor_id(access.context)
        artifact_id = uuid5(
            NAMESPACE_URL,
            f"artifact/{access.context.tenant_id}/{owner_user_id}/{idempotency_key}",
        )
        now = datetime.now(UTC)
        record = await self._store.create_upload(
            access.context,
            artifact_id=artifact_id,
            owner_user_id=owner_user_id,
            request=request,
            quarantine_object_uri=(
                f"quarantine://tenant/{access.context.tenant_id}/"
                f"artifact/{artifact_id}/source"
            ),
            upload_expires_at=now + ARTIFACT_UPLOAD_TTL,
            expires_at=now + ARTIFACT_RETENTION,
            idempotency_key=idempotency_key,
            request_hash=canonical_request_hash("artifact.upload.create", request),
            metadata=metadata,
        )
        if record.status != "UPLOADING" or record.upload_expires_at <= now:
            await self._store.fail_upload(
                access.context,
                artifact_id=record.id,
                owner_user_id=owner_user_id,
                code="ARTIFACT_UPLOAD_EXPIRED",
                now=now,
            )
            raise resource_state_conflict("The Artifact upload window has expired.")
        try:
            grant = await self._objects.create_upload_grant(
                access.context, artifact=record
            )
        except ArtifactObjectStoreUnavailable as error:
            raise dependency_unavailable(
                "Artifact object storage is unavailable."
            ) from error
        _validate_upload_grant(grant, record, self._url_policy)
        return ArtifactUploadAccepted(
            artifact_id=str(record.id),
            upload_url=grant.upload_url,
            expires_at=grant.expires_at,
            required_headers=grant.required_headers,
        )

    async def get_artifact(
        self,
        principal: AuthenticatedPrincipal,
        *,
        artifact_id: str,
        metadata: RequestMetadata,
    ) -> Artifact:
        access = await self._access(principal, "read", metadata)
        record = await self._store.get_owned(
            access.context,
            artifact_id=_resource_id(artifact_id),
            owner_user_id=_actor_id(access.context),
        )
        if record is None:
            raise resource_not_found()
        return artifact_response(record)

    async def complete_upload(
        self,
        principal: AuthenticatedPrincipal,
        *,
        artifact_id: str,
        request: ArtifactCompleteRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> Artifact:
        access = await self._access(principal, "create", metadata)
        owner_user_id = _actor_id(access.context)
        resource_id = _resource_id(artifact_id)
        record = await self._store.get_owned(
            access.context,
            artifact_id=resource_id,
            owner_user_id=owner_user_id,
        )
        if record is None:
            raise resource_not_found()
        _require_completion_matches(record, request)
        now = datetime.now(UTC)
        request_hash = canonical_request_hash(
            "artifact.upload.complete",
            request,
            extra={"artifact_id": str(record.id)},
        )
        if record.status != "UPLOADING":
            if record.status in {"SCANNING", "AVAILABLE", "REJECTED", "FAILED"}:
                replayed = await self._store.mark_scanning(
                    access.context,
                    artifact_id=record.id,
                    owner_user_id=owner_user_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    metadata=metadata,
                    now=now,
                )
                if replayed is None:
                    raise resource_not_found()
                return artifact_response(replayed)
            raise resource_state_conflict(
                "The Artifact cannot be completed in its current state."
            )
        if record.upload_expires_at <= now:
            await self._store.fail_upload(
                access.context,
                artifact_id=record.id,
                owner_user_id=owner_user_id,
                code="ARTIFACT_UPLOAD_EXPIRED",
                now=now,
            )
            raise PlatformError(
                status_code=409,
                code="ARTIFACT_UPLOAD_EXPIRED",
                message="The Artifact upload window has expired.",
            )
        try:
            observation = await self._objects.inspect_quarantine(
                access.context, artifact=record
            )
        except ArtifactObjectNotFound as error:
            raise PlatformError(
                status_code=409,
                code="ARTIFACT_UPLOAD_NOT_FOUND",
                message="The uploaded Artifact object was not found.",
            ) from error
        except ArtifactObjectStoreUnavailable as error:
            raise dependency_unavailable(
                "Artifact object storage is unavailable."
            ) from error
        try:
            _require_observation_matches(record, observation)
        except PlatformError:
            await self._store.fail_upload(
                access.context,
                artifact_id=record.id,
                owner_user_id=owner_user_id,
                code="ARTIFACT_UPLOAD_MISMATCH",
                now=now,
            )
            raise
        updated = await self._store.mark_scanning(
            access.context,
            artifact_id=record.id,
            owner_user_id=owner_user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            metadata=metadata,
            now=now,
        )
        if updated is None:
            raise resource_not_found()
        return artifact_response(updated)

    async def create_download(
        self,
        principal: AuthenticatedPrincipal,
        *,
        artifact_id: str,
        metadata: RequestMetadata,
    ) -> ArtifactDownload:
        access = await self._access(principal, "download", metadata)
        now = datetime.now(UTC)
        record = await self._store.get_downloadable(
            access.context,
            artifact_id=_resource_id(artifact_id),
            owner_user_id=_actor_id(access.context),
            metadata=metadata,
            now=now,
        )
        if record is None:
            raise resource_not_found()
        if record.run_id is not None and not access.allows("run", "read"):
            raise permission_denied()
        if record.status == "EXPIRED":
            raise PlatformError(
                status_code=409,
                code="ARTIFACT_EXPIRED",
                message="The Artifact retention period has expired.",
            )
        if record.status != "AVAILABLE":
            raise resource_state_conflict("The Artifact is not available for download.")
        if record.object_uri != artifact_uri(
            tenant_id=record.tenant_id, artifact_id=record.id
        ):
            raise RuntimeError("Artifact trusted object URI is not canonical")
        grant_expires_at = min(now + ARTIFACT_DOWNLOAD_TTL, record.expires_at)
        try:
            grant = await self._objects.create_download_grant(
                access.context,
                artifact=record,
                expires_at=grant_expires_at,
            )
        except ArtifactObjectStoreUnavailable as error:
            raise dependency_unavailable(
                "Artifact object storage is unavailable."
            ) from error
        _validate_download_grant(
            grant,
            record,
            requested_expires_at=grant_expires_at,
            now=now,
            url_policy=self._url_policy,
        )
        confirmed = await self._store.confirm_download(
            access.context,
            artifact_id=record.id,
            owner_user_id=_actor_id(access.context),
            grant_expires_at=grant.expires_at,
            metadata=metadata,
            now=datetime.now(UTC),
        )
        if not confirmed:
            raise resource_state_conflict(
                "The Artifact download authorization was revoked."
            )
        return ArtifactDownload(url=grant.url, expires_at=grant.expires_at)

    async def delete_artifact(
        self,
        principal: AuthenticatedPrincipal,
        *,
        artifact_id: str,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> OperationAccepted:
        access = await self._access(principal, "delete", metadata)
        outcome = await self._store.request_delete(
            access.context,
            artifact_id=_resource_id(artifact_id),
            owner_user_id=_actor_id(access.context),
            idempotency_key=idempotency_key,
            request_hash=canonical_request_hash(
                "artifact.delete", None, extra={"artifact_id": artifact_id}
            ),
            metadata=metadata,
            now=datetime.now(UTC),
        )
        if outcome is None:
            raise resource_not_found()
        if outcome.replay is not None:
            return OperationAccepted.model_validate(outcome.replay.response_body)
        if outcome.value is None:
            raise RuntimeError("Artifact delete outcome is missing its operation")
        return OperationAccepted(
            operation_id=str(outcome.value.id),
            status="ACCEPTED",
            status_url=f"/api/v1/operations/{outcome.value.id}",
        )

    async def _access(
        self,
        principal: AuthenticatedPrincipal,
        action: str,
        metadata: RequestMetadata,
    ) -> TenantAccess:
        access = await self._access_resolver.resolve_tenant_access(principal, metadata)
        if not access.allows("artifact", action):
            raise permission_denied()
        return access


def artifact_response(record: ArtifactRecord) -> Artifact:
    return Artifact(
        id=str(record.id),
        name=record.name,
        status=record.status,
        size=record.size_bytes,
        content_type=record.content_type,
        hash=record.content_hash,
        run_id=str(record.run_id) if record.run_id is not None else None,
        expires_at=record.expires_at,
        created_at=record.created_at,
    )


def _validate_upload_request(request: ArtifactUploadCreateRequest) -> None:
    if request.name in {".", ".."} or any(
        character in request.name for character in ("/", "\\", "\x00")
    ):
        raise validation_error("Artifact name must be a single safe filename.")
    if any(ord(character) < 32 or ord(character) == 127 for character in request.name):
        raise validation_error("Artifact name contains a control character.")
    if _CONTENT_TYPE_PATTERN.fullmatch(request.content_type) is None:
        raise validation_error("Artifact Content-Type is invalid.")


def _validate_upload_grant(
    grant: ArtifactUploadGrant,
    record: ArtifactRecord,
    url_policy: ArtifactGrantUrlPolicy,
) -> None:
    url_policy.validate(grant.upload_url)
    if grant.expires_at > record.upload_expires_at:
        raise RuntimeError("Artifact upload grant exceeds the frozen upload window")
    offset = grant.expires_at.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise RuntimeError("Artifact upload grant expiration must be UTC")
    if grant.expires_at <= datetime.now(UTC):
        raise RuntimeError("Artifact object store returned an expired upload grant")
    if len(grant.required_headers) > 20 or any(
        not key
        or len(key) > 128
        or _HEADER_NAME_PATTERN.fullmatch(key) is None
        or key.lower() in _FORBIDDEN_UPLOAD_HEADERS
        or key.lower().startswith(("x-forwarded-", "x-proxy-"))
        or len(value) > 4096
        or "\r" in key + value
        or "\n" in key + value
        for key, value in grant.required_headers.items()
    ):
        raise RuntimeError("Artifact upload grant headers are unsafe")
    content_type = next(
        (
            value
            for key, value in grant.required_headers.items()
            if key.lower() == "content-type"
        ),
        None,
    )
    if content_type is not None and content_type != record.content_type:
        raise RuntimeError("Artifact upload grant Content-Type drifted")


def _validate_download_grant(
    grant: ArtifactDownloadGrant,
    record: ArtifactRecord,
    *,
    requested_expires_at: datetime,
    now: datetime,
    url_policy: ArtifactGrantUrlPolicy,
) -> None:
    if grant.artifact_id != record.id:
        raise RuntimeError("Artifact object store returned an unsafe download URL")
    url_policy.validate(grant.url)
    offset = grant.expires_at.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise RuntimeError("Artifact download grant expiration must be UTC")
    if (
        grant.expires_at <= now
        or grant.expires_at > requested_expires_at
        or grant.expires_at > record.expires_at
    ):
        raise RuntimeError("Artifact download grant exceeds its authorization window")


def _require_completion_matches(
    record: ArtifactRecord, request: ArtifactCompleteRequest
) -> None:
    if request.size != record.size_bytes or request.content_hash != record.content_hash:
        raise PlatformError(
            status_code=409,
            code="ARTIFACT_UPLOAD_MISMATCH",
            message="Artifact completion does not match the upload reservation.",
        )


def _require_observation_matches(
    record: ArtifactRecord, observation: ArtifactObjectObservation
) -> None:
    if (
        observation.size_bytes != record.size_bytes
        or observation.content_hash != record.content_hash
        or observation.content_type != record.content_type
    ):
        raise PlatformError(
            status_code=409,
            code="ARTIFACT_UPLOAD_MISMATCH",
            message="Uploaded Artifact metadata does not match the reservation.",
        )


def _resource_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as error:
        raise validation_error("Resource identifier is invalid.") from error


def _actor_id(context: TenantContext) -> UUID:
    return _resource_id(context.subject_id)

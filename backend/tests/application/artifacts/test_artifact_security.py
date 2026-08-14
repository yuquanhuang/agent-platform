"""Archive, signed URL, and quarantine reader security tests."""

from __future__ import annotations

import io
import stat
import tarfile
import zipfile
from datetime import UTC, datetime, timedelta
from typing import BinaryIO, cast
from uuid import UUID

import pytest

from packages.application.artifacts import (
    ArchiveAwareArtifactSecurityScanner,
    ArtifactArchiveInspector,
    ArtifactArchiveLimits,
    ArtifactGrantUrlPolicy,
    ArtifactScanVerdict,
)
from packages.contracts.public import SubjectType, TenantContext
from packages.domain.public import ArtifactRecord, ArtifactStatus

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
ARTIFACT_ID = UUID("33333333-3333-4333-8333-333333333333")
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _zip(entries: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
        if symlink is not None:
            info = zipfile.ZipInfo(symlink)
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "target.txt")
    return output.getvalue()


def _tar(
    *, name: str, content: bytes = b"safe", entry_type: bytes | None = None
) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        info = tarfile.TarInfo(name)
        if entry_type is not None:
            info.type = entry_type
            if entry_type in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
                info.linkname = "target.txt"
            archive.addfile(info)
        else:
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


@pytest.mark.parametrize(
    "member",
    ["../secret.txt", "/etc/passwd", "C:\\secret.txt", "safe/../../secret"],
)
def test_zip_slip_paths_are_rejected(member: str) -> None:
    inspection = ArtifactArchiveInspector().inspect(
        io.BytesIO(_zip({member: b"unsafe"})),
        name="result.zip",
        content_type="application/zip",
    )

    assert inspection.decision == "REJECTED"
    assert "ARCHIVE_UNSAFE_PATH" in inspection.findings


@pytest.mark.parametrize(
    "entry_type",
    [
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
        tarfile.CHRTYPE,
        tarfile.BLKTYPE,
        tarfile.FIFOTYPE,
        b"s",
    ],
)
def test_archive_links_special_entries_and_duplicate_paths_are_rejected(
    entry_type: bytes,
) -> None:
    inspector = ArtifactArchiveInspector()
    zip_inspection = inspector.inspect(
        io.BytesIO(_zip({"Report.txt": b"one", "report.txt": b"two"}, symlink="link")),
        name="result.zip",
        content_type="application/zip",
    )
    tar_inspection = inspector.inspect(
        io.BytesIO(_tar(name="special", entry_type=entry_type)),
        name="result.tar.gz",
        content_type="application/gzip",
    )

    assert zip_inspection.decision == "REJECTED"
    assert "ARCHIVE_DUPLICATE_PATH" in zip_inspection.findings
    assert "ARCHIVE_LINK_ENTRY" in zip_inspection.findings
    assert tar_inspection.findings == ("ARCHIVE_SPECIAL_ENTRY",)


def test_archive_bomb_nested_archive_and_many_files_are_rejected() -> None:
    nested = _zip({"inner.txt": b"safe"})
    bomb_limits = ArtifactArchiveLimits(
        max_entries=2,
        max_single_file_bytes=1024 * 1024,
        max_expanded_bytes=1024 * 1024,
        max_compression_ratio=5,
    )
    inspection = ArtifactArchiveInspector(bomb_limits).inspect(
        io.BytesIO(
            _zip(
                {
                    "nested.bin": nested,
                    "zeros.txt": b"0" * 100_000,
                    "third.txt": b"third",
                }
            )
        ),
        name="result.zip",
        content_type="application/zip",
    )

    assert inspection.decision == "REJECTED"
    assert "ARCHIVE_TOO_MANY_ENTRIES" in inspection.findings
    assert "ARCHIVE_COMPRESSION_RATIO_EXCEEDED" in inspection.findings
    assert "ARCHIVE_NESTED_ARCHIVE" in inspection.findings


def test_encrypted_zip_entry_is_rejected_without_attempting_decryption() -> None:
    encrypted = bytearray(_zip({"secret.txt": b"opaque"}))
    local_header = encrypted.index(b"PK\x03\x04")
    central_header = encrypted.index(b"PK\x01\x02")
    encrypted[local_header + 6] |= 0x1
    encrypted[central_header + 8] |= 0x1

    inspection = ArtifactArchiveInspector().inspect(
        io.BytesIO(encrypted),
        name="encrypted.zip",
        content_type="application/zip",
    )

    assert inspection.findings == ("ARCHIVE_ENCRYPTED_ENTRY",)


def test_valid_zip_tar_and_plain_file_pass_without_host_extraction() -> None:
    inspector = ArtifactArchiveInspector()

    assert (
        inspector.inspect(
            io.BytesIO(_zip({"reports/result.txt": b"safe"})),
            name="result.zip",
            content_type="application/zip",
        ).decision
        == "PASSED"
    )
    assert (
        inspector.inspect(
            io.BytesIO(_tar(name="reports/result.txt")),
            name="opaque.bin",
            content_type="application/octet-stream",
        ).decision
        == "PASSED"
    )
    assert (
        inspector.inspect(
            io.BytesIO(b"plain result"),
            name="result.txt",
            content_type="text/plain",
        ).decision
        == "PASSED"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "https://127.0.0.1/object",
        "https://[::1]/object",
        "https://localhost/object",
        "https://objects.test.evil.example/object",
    ],
)
def test_signed_url_policy_rejects_metadata_local_and_origin_bypass(url: str) -> None:
    policy = ArtifactGrantUrlPolicy(frozenset({"https://objects.test"}))

    with pytest.raises(RuntimeError):
        policy.validate(url)


def test_signed_url_policy_allows_only_exact_configured_origin() -> None:
    policy = ArtifactGrantUrlPolicy(
        frozenset({"https://objects.test", "http://minio.internal:9000"})
    )

    policy.validate("https://objects.test/download?signature=opaque")
    policy.validate("http://minio.internal:9000/upload?signature=opaque")


class ScannerStub:
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def scan(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> ArtifactScanVerdict:
        return ArtifactScanVerdict(
            decision="PASSED",
            engine="malware-test",
            definition_version="1",
            findings=(),
            scanned_at=NOW,
        )

    async def copy_quarantine(
        self,
        context: TenantContext,
        *,
        artifact: ArtifactRecord,
        destination: BinaryIO,
        max_bytes: int,
    ) -> int:
        assert len(self.content) <= max_bytes
        destination.write(self.content)
        return len(self.content)


@pytest.mark.asyncio
async def test_archive_scanner_rejects_hostile_quarantine_content() -> None:
    content = _zip({"../secret.txt": b"unsafe"})
    stub = ScannerStub(content)
    scanner = ArchiveAwareArtifactSecurityScanner(stub, stub)

    verdict = await scanner.scan(_context(), artifact=_artifact(content))

    assert verdict.decision == "REJECTED"
    assert verdict.engine == "archive-inspector"
    assert verdict.findings == ("ARCHIVE_UNSAFE_PATH",)


def _context() -> TenantContext:
    return TenantContext(
        tenant_id=str(TENANT_ID),
        subject_type=SubjectType.SERVICE,
        subject_id=str(ACTOR_ID),
        auth_time=NOW,
        request_id="req-artifact-security",
        trace_id="trace-artifact-security",
    )


def _artifact(content: bytes) -> ArtifactRecord:
    return ArtifactRecord(
        id=ARTIFACT_ID,
        tenant_id=TENANT_ID,
        workspace_id=None,
        run_id=None,
        owner_user_id=ACTOR_ID,
        name="result.zip",
        quarantine_object_uri=(
            f"quarantine://tenant/{TENANT_ID}/artifact/{ARTIFACT_ID}/source"
        ),
        object_uri=None,
        content_hash="sha256:" + "a" * 64,
        size_bytes=len(content),
        content_type="application/zip",
        status=cast(ArtifactStatus, "SCANNING"),
        required_output=False,
        scan_result=None,
        upload_expires_at=NOW - timedelta(minutes=1),
        created_at=NOW - timedelta(minutes=10),
        updated_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(days=30),
        retention_delete_after=None,
        deleted_at=None,
    )

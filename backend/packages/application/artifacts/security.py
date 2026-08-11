"""Artifact archive and signed-URL security policies."""

from __future__ import annotations

import os
import re
import stat
import tarfile
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from typing import BinaryIO, Literal, Protocol, cast

from packages.application.artifacts.scanning import (
    ArtifactScanVerdict,
    ArtifactSecurityScanner,
    RetryableArtifactScanError,
)
from packages.contracts.public import TenantContext
from packages.domain.public import ArtifactRecord

_ARCHIVE_SUFFIXES = (
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
)
_ARCHIVE_CONTENT_TYPES = frozenset(
    {
        "application/zip",
        "application/x-zip-compressed",
        "application/x-tar",
        "application/gzip",
        "application/x-gzip",
        "application/x-bzip2",
        "application/x-xz",
    }
)
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True, slots=True)
class ArtifactArchiveLimits:
    """Bound metadata expansion before any archive member is read."""

    max_archive_bytes: int = 100 * 1024 * 1024
    max_entries: int = 2_000
    max_single_file_bytes: int = 100 * 1024 * 1024
    max_expanded_bytes: int = 512 * 1024 * 1024
    max_compression_ratio: float = 100.0
    spool_memory_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        if (
            self.max_archive_bytes < 1
            or self.max_entries < 1
            or self.max_single_file_bytes < 1
            or self.max_expanded_bytes < 1
            or self.max_compression_ratio <= 0
            or self.spool_memory_bytes < 1
        ):
            raise ValueError("Artifact archive limits must be positive")


@dataclass(frozen=True, slots=True)
class ArtifactArchiveInspection:
    decision: Literal["PASSED", "REJECTED"]
    findings: tuple[str, ...]


class ArtifactQuarantineContentReader(Protocol):
    async def copy_quarantine(
        self,
        context: TenantContext,
        *,
        artifact: ArtifactRecord,
        destination: BinaryIO,
        max_bytes: int,
    ) -> int:
        """Copy one immutable quarantine object into a bounded destination."""

        ...


class ArtifactArchiveInspector:
    """Inspect ZIP/TAR metadata without extracting into the host filesystem."""

    def __init__(self, limits: ArtifactArchiveLimits | None = None) -> None:
        self._limits = limits or ArtifactArchiveLimits()

    @property
    def limits(self) -> ArtifactArchiveLimits:
        return self._limits

    def inspect(
        self,
        source: BinaryIO,
        *,
        name: str,
        content_type: str,
    ) -> ArtifactArchiveInspection:
        archive_size = _stream_size(source)
        if archive_size > self._limits.max_archive_bytes:
            return _rejected("ARCHIVE_INPUT_TOO_LARGE")
        archive_kind = _archive_kind(source, name=name, content_type=content_type)
        if archive_kind is None:
            return ArtifactArchiveInspection(decision="PASSED", findings=())
        try:
            if archive_kind == "zip":
                findings = self._inspect_zip(source, archive_size=archive_size)
            else:
                findings = self._inspect_tar(source, archive_size=archive_size)
        except (OSError, EOFError, tarfile.TarError, zipfile.BadZipFile):
            return _rejected("ARCHIVE_INVALID")
        if findings:
            return ArtifactArchiveInspection(
                decision="REJECTED", findings=tuple(sorted(findings))
            )
        return ArtifactArchiveInspection(decision="PASSED", findings=())

    def _inspect_zip(self, source: BinaryIO, *, archive_size: int) -> set[str]:
        source.seek(0)
        findings: set[str] = set()
        normalized_paths: set[str] = set()
        expanded_bytes = 0
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > self._limits.max_entries:
                findings.add("ARCHIVE_TOO_MANY_ENTRIES")
            for entry in entries[: self._limits.max_entries + 1]:
                normalized = _safe_member_path(entry.filename)
                if normalized is None:
                    findings.add("ARCHIVE_UNSAFE_PATH")
                elif normalized in normalized_paths:
                    findings.add("ARCHIVE_DUPLICATE_PATH")
                else:
                    normalized_paths.add(normalized)
                unix_mode = (entry.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(unix_mode):
                    findings.add("ARCHIVE_LINK_ENTRY")
                if entry.flag_bits & 0x1:
                    findings.add("ARCHIVE_ENCRYPTED_ENTRY")
                if entry.is_dir():
                    continue
                expanded_bytes += entry.file_size
                if entry.file_size > self._limits.max_single_file_bytes:
                    findings.add("ARCHIVE_MEMBER_TOO_LARGE")
                if _ratio(entry.file_size, entry.compress_size) > (
                    self._limits.max_compression_ratio
                ):
                    findings.add("ARCHIVE_COMPRESSION_RATIO_EXCEEDED")
                if _looks_nested_name(entry.filename):
                    findings.add("ARCHIVE_NESTED_ARCHIVE")
                if not findings.intersection(
                    {
                        "ARCHIVE_ENCRYPTED_ENTRY",
                        "ARCHIVE_COMPRESSION_RATIO_EXCEEDED",
                    }
                ):
                    with archive.open(entry) as member:
                        if _looks_like_archive(member.read(512)):
                            findings.add("ARCHIVE_NESTED_ARCHIVE")
            _add_aggregate_limit_findings(
                findings,
                expanded_bytes=expanded_bytes,
                archive_size=archive_size,
                limits=self._limits,
            )
        return findings

    def _inspect_tar(self, source: BinaryIO, *, archive_size: int) -> set[str]:
        source.seek(0)
        findings: set[str] = set()
        normalized_paths: set[str] = set()
        expanded_bytes = 0
        entry_count = 0
        with tarfile.open(fileobj=source, mode="r:*") as archive:
            for entry in archive:
                entry_count += 1
                if entry_count > self._limits.max_entries:
                    findings.add("ARCHIVE_TOO_MANY_ENTRIES")
                    break
                normalized = _safe_member_path(entry.name)
                if normalized is None:
                    findings.add("ARCHIVE_UNSAFE_PATH")
                elif normalized in normalized_paths:
                    findings.add("ARCHIVE_DUPLICATE_PATH")
                else:
                    normalized_paths.add(normalized)
                if not (entry.isfile() or entry.isdir()):
                    findings.add("ARCHIVE_SPECIAL_ENTRY")
                    continue
                if entry.isdir():
                    continue
                expanded_bytes += entry.size
                if entry.size > self._limits.max_single_file_bytes:
                    findings.add("ARCHIVE_MEMBER_TOO_LARGE")
                if _looks_nested_name(entry.name):
                    findings.add("ARCHIVE_NESTED_ARCHIVE")
                member = archive.extractfile(entry)
                if member is not None:
                    with member:
                        if _looks_like_archive(member.read(512)):
                            findings.add("ARCHIVE_NESTED_ARCHIVE")
            _add_aggregate_limit_findings(
                findings,
                expanded_bytes=expanded_bytes,
                archive_size=archive_size,
                limits=self._limits,
            )
        return findings


class ArchiveAwareArtifactSecurityScanner:
    """Compose malware/content scanning with bounded archive inspection."""

    def __init__(
        self,
        scanner: ArtifactSecurityScanner,
        reader: ArtifactQuarantineContentReader,
        inspector: ArtifactArchiveInspector | None = None,
    ) -> None:
        self._scanner = scanner
        self._reader = reader
        self._inspector = inspector or ArtifactArchiveInspector()

    async def scan(
        self, context: TenantContext, *, artifact: ArtifactRecord
    ) -> ArtifactScanVerdict:
        base_verdict = await self._scanner.scan(context, artifact=artifact)
        if base_verdict.decision == "REJECTED":
            return base_verdict
        limits = self._inspector.limits
        with tempfile.SpooledTemporaryFile(
            max_size=limits.spool_memory_bytes, mode="w+b"
        ) as content:
            binary_content = cast(BinaryIO, content)
            try:
                copied = await self._reader.copy_quarantine(
                    context,
                    artifact=artifact,
                    destination=binary_content,
                    max_bytes=limits.max_archive_bytes,
                )
            except RetryableArtifactScanError:
                raise
            except (OSError, TimeoutError) as error:
                raise RetryableArtifactScanError(
                    "Artifact quarantine content is unavailable"
                ) from error
            if copied != artifact.size_bytes or copied > limits.max_archive_bytes:
                return ArtifactScanVerdict(
                    decision="REJECTED",
                    engine="archive-inspector",
                    definition_version="archive-v1",
                    findings=("ARCHIVE_SOURCE_SIZE_MISMATCH",),
                    scanned_at=base_verdict.scanned_at,
                )
            inspection = self._inspector.inspect(
                binary_content,
                name=artifact.name,
                content_type=artifact.content_type,
            )
        if inspection.decision == "PASSED":
            return base_verdict
        return ArtifactScanVerdict(
            decision="REJECTED",
            engine="archive-inspector",
            definition_version="archive-v1",
            findings=inspection.findings,
            scanned_at=base_verdict.scanned_at,
        )


def _stream_size(source: BinaryIO) -> int:
    current = source.tell()
    source.seek(0, os.SEEK_END)
    size = source.tell()
    source.seek(current)
    return size


def _archive_kind(
    source: BinaryIO, *, name: str, content_type: str
) -> Literal["zip", "tar"] | None:
    source.seek(0)
    prefix = source.read(512)
    source.seek(0)
    lowered_name = name.lower()
    if prefix.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "zip"
    if len(prefix) >= 265 and prefix[257:262] == b"ustar":
        return "tar"
    if prefix.startswith((b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00")):
        return "tar"
    declared_archive = content_type.lower() in _ARCHIVE_CONTENT_TYPES or any(
        lowered_name.endswith(suffix) for suffix in _ARCHIVE_SUFFIXES
    )
    return "tar" if declared_archive else None


def _safe_member_path(name: str) -> str | None:
    if not name or "\x00" in name or any(ord(character) < 32 for character in name):
        return None
    normalized = unicodedata.normalize("NFC", name.replace("\\", "/"))
    if normalized.startswith(("/", "//")) or _WINDOWS_DRIVE.match(normalized):
        return None
    parts = normalized.split("/")
    if parts[-1] == "":
        parts.pop()
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    return "/".join(parts).casefold()


def _looks_nested_name(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.endswith(suffix) for suffix in _ARCHIVE_SUFFIXES)


def _looks_like_archive(prefix: bytes) -> bool:
    return prefix.startswith(
        (
            b"PK\x03\x04",
            b"PK\x05\x06",
            b"\x1f\x8b",
            b"BZh",
            b"\xfd7zXZ\x00",
        )
    ) or (len(prefix) >= 265 and prefix[257:262] == b"ustar")


def _ratio(expanded_bytes: int, compressed_bytes: int) -> float:
    if expanded_bytes == 0:
        return 0.0
    return expanded_bytes / max(compressed_bytes, 1)


def _add_aggregate_limit_findings(
    findings: set[str],
    *,
    expanded_bytes: int,
    archive_size: int,
    limits: ArtifactArchiveLimits,
) -> None:
    if expanded_bytes > limits.max_expanded_bytes:
        findings.add("ARCHIVE_EXPANDED_SIZE_EXCEEDED")
    if _ratio(expanded_bytes, archive_size) > limits.max_compression_ratio:
        findings.add("ARCHIVE_COMPRESSION_RATIO_EXCEEDED")


def _rejected(finding: str) -> ArtifactArchiveInspection:
    return ArtifactArchiveInspection(decision="REJECTED", findings=(finding,))

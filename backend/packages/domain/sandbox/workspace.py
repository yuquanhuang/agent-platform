"""Canonical Workspace URIs and immutable tenant-scoped workspace facts."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

WorkspaceStatus = Literal["ACTIVE", "SEALED", "QUARANTINED", "DELETING", "DELETED"]

WORKSPACE_STATUSES = frozenset(
    {"ACTIVE", "SEALED", "QUARANTINED", "DELETING", "DELETED"}
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_MAX_URI_LENGTH = 4096


@dataclass(frozen=True, slots=True)
class WorkspaceUri:
    """A canonical logical path; it never contains a physical filesystem path."""

    tenant_id: str
    user_id: str
    session_id: str
    run_id: str
    relative_path: tuple[str, ...] = ()

    @classmethod
    def root(
        cls, *, tenant_id: str, user_id: str, session_id: str, run_id: str
    ) -> WorkspaceUri:
        for name, value in (
            ("tenant_id", tenant_id),
            ("user_id", user_id),
            ("session_id", session_id),
            ("run_id", run_id),
        ):
            _validate_identifier(value, name)
        return cls(tenant_id, user_id, session_id, run_id)

    @classmethod
    def parse(cls, value: str) -> WorkspaceUri:
        if len(value) == 0 or len(value) > _MAX_URI_LENGTH:
            raise ValueError("Workspace URI length is invalid")
        if "%" in value or "\\" in value or "\x00" in value:
            raise ValueError("Workspace URI contains an unsupported escape")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("Workspace URI contains a control character")
        if not value.startswith("workspace://"):
            raise ValueError("Workspace URI must use the workspace scheme")
        if not value.startswith("workspace://tenant/"):
            raise ValueError("Workspace URI authority is invalid")
        tenant, slash, path = value.removeprefix("workspace://tenant/").partition("/")
        _validate_identifier(tenant, "tenant_id")
        if not slash or not path:
            raise ValueError("Workspace URI path is incomplete")
        if "?" in path or "#" in path:
            raise ValueError("Workspace URI cannot contain query or fragment")
        segments = path.split("/")
        if segments[-1] == "":
            segments.pop()
        if len(segments) < 6 or segments[:1] != ["user"]:
            raise ValueError("Workspace URI identity path is invalid")
        if segments[2:3] != ["session"] or segments[4:5] != ["runs"]:
            raise ValueError("Workspace URI identity path is invalid")
        user_id, session_id, run_id = segments[1], segments[3], segments[5]
        for name, identifier in (
            ("user_id", user_id),
            ("session_id", session_id),
            ("run_id", run_id),
        ):
            _validate_identifier(identifier, name)
        relative = tuple(_validate_segment(segment) for segment in segments[6:])
        parsed = cls(tenant, user_id, session_id, run_id, relative)
        if parsed.to_string() != value:
            raise ValueError("Workspace URI is not canonical")
        return parsed

    def child(self, *segments: str) -> WorkspaceUri:
        return WorkspaceUri(
            self.tenant_id,
            self.user_id,
            self.session_id,
            self.run_id,
            self.relative_path
            + tuple(_validate_segment(segment) for segment in segments),
        )

    def is_within(self, candidate: WorkspaceUri) -> bool:
        return (
            self.tenant_id == candidate.tenant_id
            and self.user_id == candidate.user_id
            and self.session_id == candidate.session_id
            and self.run_id == candidate.run_id
            and candidate.relative_path[: len(self.relative_path)] == self.relative_path
        )

    def is_root(self) -> bool:
        return not self.relative_path

    def to_string(self) -> str:
        identity = (
            f"workspace://tenant/{self.tenant_id}/user/{self.user_id}/"
            f"session/{self.session_id}/runs/{self.run_id}"
        )
        if not self.relative_path:
            return identity + "/"
        return identity + "/" + "/".join(self.relative_path) + "/"


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    id: UUID
    tenant_id: UUID
    user_id: UUID
    session_id: UUID
    run_id: UUID
    uri: str
    quota_bytes: int
    used_bytes: int
    max_files: int
    file_count: int
    max_file_bytes: int
    status: WorkspaceStatus
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


def ensure_workspace_transition(
    current: WorkspaceStatus, target: WorkspaceStatus
) -> None:
    if current == target:
        return
    allowed: dict[WorkspaceStatus, frozenset[WorkspaceStatus]] = {
        "ACTIVE": frozenset({"SEALED", "QUARANTINED", "DELETING"}),
        "SEALED": frozenset({"QUARANTINED", "DELETING"}),
        "QUARANTINED": frozenset({"DELETING"}),
        "DELETING": frozenset({"DELETED", "QUARANTINED"}),
        "DELETED": frozenset(),
    }
    if target not in allowed[current]:
        raise ValueError(f"Workspace transition {current} -> {target} is not allowed")


def ensure_workspace_usage(
    *, quota_bytes: int, used_bytes: int, max_files: int, file_count: int
) -> None:
    if quota_bytes <= 0 or used_bytes < 0 or used_bytes > quota_bytes:
        raise ValueError("Workspace byte usage exceeds its quota")
    if max_files <= 0 or file_count < 0 or file_count > max_files:
        raise ValueError("Workspace file count exceeds its quota")


def _validate_identifier(value: str, field_name: str) -> None:
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field_name} is not a valid Workspace identifier")


def _validate_segment(value: str) -> str:
    if not value or value in {".", ".."}:
        raise ValueError("Workspace relative path contains an invalid segment")
    normalized = unicodedata.normalize("NFC", value)
    if normalized != value:
        raise ValueError("Workspace relative path must use NFC Unicode")
    if "\\" in value or "\x00" in value:
        raise ValueError("Workspace relative path contains an invalid character")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Workspace relative path contains a control character")
    if len(value) > 255 or "/" in value:
        raise ValueError("Workspace relative path segment is too long or nested")
    return value

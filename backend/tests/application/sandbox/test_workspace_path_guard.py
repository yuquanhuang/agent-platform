"""Workspace path traversal, link, file-type, and TOCTOU safety tests."""

import os
import socket
from pathlib import Path

import pytest

from packages.application.sandbox import WorkspacePathError, WorkspacePathGuard
from packages.domain.public import WorkspaceUri

ROOT = WorkspaceUri.root(
    tenant_id="11111111-1111-4111-8111-111111111111",
    user_id="22222222-2222-4222-8222-222222222222",
    session_id="33333333-3333-4333-8333-333333333333",
    run_id="44444444-4444-4444-8444-444444444444",
)


@pytest.fixture
def root_fd(tmp_path: Path):
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def test_guard_opens_regular_file_by_descriptor(root_fd: int, tmp_path: Path) -> None:
    guard = WorkspacePathGuard()
    guard.ensure_directory(root_fd, ROOT.child("work"))
    (tmp_path / "work" / "result.txt").write_bytes(b"safe-result")

    with guard.open_file(
        root_fd,
        ROOT,
        ROOT.child("work", "result.txt"),
        max_file_bytes=1024,
    ) as (handle, metadata):
        assert handle.read() == b"safe-result"
        assert metadata.size_bytes == 11


def test_guard_rejects_cross_workspace_symlink_hardlink_and_fifo(
    root_fd: int, tmp_path: Path
) -> None:
    guard = WorkspacePathGuard()
    guard.ensure_directory(root_fd, ROOT.child("work"))
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (tmp_path / "work" / "escape").symlink_to(outside)
    os.link(outside, tmp_path / "work" / "hardlink")
    os.mkfifo(tmp_path / "work" / "pipe")

    for name in ("escape", "hardlink", "pipe"):
        with (
            pytest.raises(WorkspacePathError),
            guard.open_file(
                root_fd,
                ROOT,
                ROOT.child("work", name),
                max_file_bytes=1024,
            ),
        ):
            pass


def test_guard_rejects_open_unix_socket_descriptor() -> None:
    guard = WorkspacePathGuard()
    reader, writer = socket.socketpair()
    try:
        with pytest.raises(WorkspacePathError, match="file type"):
            guard.validate_open_file(reader.fileno(), max_file_bytes=1024)
    finally:
        reader.close()
        writer.close()


def test_guard_rejects_symlink_directory_and_oversized_file(
    root_fd: int, tmp_path: Path
) -> None:
    guard = WorkspacePathGuard()
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    (tmp_path / "large.bin").write_bytes(b"x" * 5)

    with pytest.raises(WorkspacePathError):
        guard.ensure_directory(root_fd, ROOT.child("linked", "nested"))
    with (
        pytest.raises(WorkspacePathError, match="per-file quota"),
        guard.open_file(root_fd, ROOT, ROOT.child("large.bin"), max_file_bytes=4),
    ):
        pass


def test_open_descriptor_is_stable_if_path_is_replaced_after_validation(
    root_fd: int, tmp_path: Path
) -> None:
    guard = WorkspacePathGuard()
    original = tmp_path / "result.txt"
    original.write_bytes(b"original")
    replacement = tmp_path / "replacement.txt"
    replacement.write_bytes(b"replacement")

    with guard.open_file(
        root_fd, ROOT, ROOT.child("result.txt"), max_file_bytes=1024
    ) as (handle, _):
        original.rename(tmp_path / "result.old")
        original.symlink_to(replacement)
        assert handle.read() == b"original"

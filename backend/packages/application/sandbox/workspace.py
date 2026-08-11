"""File-descriptor based Workspace path safety for trusted Provider roots."""

from __future__ import annotations

import os
import stat
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import BinaryIO

from packages.domain.sandbox import WorkspaceUri


class WorkspacePathError(ValueError):
    """A logical Workspace path cannot be safely opened or inspected."""


@dataclass(frozen=True, slots=True)
class WorkspaceFileMetadata:
    size_bytes: int
    file_count: int = 1


class WorkspacePathGuard:
    """Resolve logical paths below an already trusted directory file descriptor."""

    def ensure_directory(self, root_fd: int, workspace: WorkspaceUri) -> None:
        fd = self._open_directory_components(
            root_fd, workspace.relative_path, create=True
        )
        os.close(fd)

    @contextmanager
    def open_file(
        self,
        root_fd: int,
        workspace_root: WorkspaceUri,
        candidate: WorkspaceUri,
        *,
        max_file_bytes: int,
    ) -> Generator[tuple[BinaryIO, WorkspaceFileMetadata], None, None]:
        relative = self._relative_components(workspace_root, candidate)
        if not relative:
            raise WorkspacePathError("A Workspace root is not a file")
        parent_fd = self._open_directory_components(
            root_fd, relative[:-1], create=False
        )
        try:
            file_fd = os.open(
                relative[-1],
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent_fd,
            )
        except OSError as error:
            raise WorkspacePathError(
                "Workspace file cannot be opened safely"
            ) from error
        finally:
            os.close(parent_fd)
        try:
            metadata = self.validate_open_file(file_fd, max_file_bytes)
            with os.fdopen(file_fd, "rb", closefd=True) as handle:
                yield handle, metadata
        except Exception:
            try:
                os.close(file_fd)
            except OSError:
                pass
            raise

    def _open_directory_components(
        self, root_fd: int, components: tuple[str, ...], *, create: bool
    ) -> int:
        try:
            current_fd = os.dup(root_fd)
            os.set_inheritable(current_fd, False)
        except OSError as error:
            raise WorkspacePathError("Workspace root is not a directory") from error
        try:
            self._verify_directory(current_fd)
        except Exception:
            os.close(current_fd)
            raise
        try:
            for component in components:
                if create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=current_fd)
                    except FileExistsError:
                        pass
                try:
                    next_fd = os.open(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                        dir_fd=current_fd,
                    )
                except OSError as error:
                    raise WorkspacePathError(
                        "Workspace path contains a missing or unsafe directory"
                    ) from error
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except Exception:
            os.close(current_fd)
            raise

    @staticmethod
    def _relative_components(
        workspace_root: WorkspaceUri, candidate: WorkspaceUri
    ) -> tuple[str, ...]:
        if not workspace_root.is_within(candidate):
            raise WorkspacePathError("Workspace path escapes its root")
        return candidate.relative_path[len(workspace_root.relative_path) :]

    @staticmethod
    def _verify_directory(fd: int) -> None:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise WorkspacePathError("Workspace root is not a directory")

    @staticmethod
    def validate_open_file(fd: int, max_file_bytes: int) -> WorkspaceFileMetadata:
        """Validate a Provider-opened descriptor before it is exported."""

        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise WorkspacePathError("Workspace file type or link count is unsafe")
        if info.st_size > max_file_bytes:
            raise WorkspacePathError("Workspace file exceeds its per-file quota")
        return WorkspaceFileMetadata(size_bytes=info.st_size)

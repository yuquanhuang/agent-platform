"""Infrastructure-neutral Sandbox Provider port and safe observation models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from packages.application.sandbox.policy import FrozenSandboxPolicy

ProviderSandboxState = Literal[
    "PROVISIONING",
    "READY",
    "RUNNING",
    "FAILED",
    "TERMINATING",
    "TERMINATED",
    "UNKNOWN",
]
ProviderProcessState = Literal[
    "STARTING",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "CANCELLING",
    "CANCELLED",
    "TERMINATED",
    "UNKNOWN",
]


@dataclass(frozen=True, slots=True)
class ProviderProvisionSpec:
    sandbox_id: str
    tenant_id: str
    run_id: str
    image_digest: str
    bundle_ref: str
    bundle_hash: str
    workspace_uri: str
    policy: FrozenSandboxPolicy


@dataclass(frozen=True, slots=True)
class ProviderProcessObservation:
    process_id: str
    state: ProviderProcessState
    exit_code: int | None = None

    def __post_init__(self) -> None:
        if not self.process_id or len(self.process_id) > 255:
            raise ValueError("process_id must contain 1 to 255 characters")


@dataclass(frozen=True, slots=True)
class ProviderSandboxObservation:
    provider_ref: str
    state: ProviderSandboxState
    observed_at: datetime
    cpu_seconds: float | None = None
    memory_bytes: int | None = None
    disk_bytes: int | None = None
    pids_current: int | None = None
    main_process: ProviderProcessObservation | None = None

    def __post_init__(self) -> None:
        if not self.provider_ref or len(self.provider_ref) > 2048:
            raise ValueError("provider_ref must contain 1 to 2048 characters")
        offset = self.observed_at.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError("observed_at must be timezone-aware UTC")
        for field_name in (
            "cpu_seconds",
            "memory_bytes",
            "disk_bytes",
            "pids_current",
        ):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} cannot be negative")


class SandboxProviderError(RuntimeError):
    """Safe Provider failure without credentials, host paths, or raw responses."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        retryable: bool,
        provider_state: ProviderSandboxState = "UNKNOWN",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.provider_state = provider_state


class SandboxProvider(Protocol):
    """Provider adapters implement isolation mechanics, never lease authority."""

    async def provision(
        self, spec: ProviderProvisionSpec
    ) -> ProviderSandboxObservation: ...

    async def inspect(self, provider_ref: str) -> ProviderSandboxObservation: ...

    async def recover(self, sandbox_id: str) -> ProviderSandboxObservation | None: ...

    async def start_process(
        self,
        provider_ref: str,
        *,
        process_id: str,
        argv: tuple[str, ...],
        working_directory: str,
        environment_refs: tuple[str, ...],
        timeout_seconds: int,
    ) -> ProviderProcessObservation: ...

    async def cancel_process(
        self, provider_ref: str, *, process_id: str, grace_seconds: int
    ) -> ProviderProcessObservation: ...

    async def terminate(
        self, provider_ref: str, *, grace_seconds: int
    ) -> ProviderSandboxObservation: ...

    async def destroy(self, provider_ref: str) -> ProviderSandboxObservation: ...

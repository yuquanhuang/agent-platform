"""Fail-closed Secret Backend selection for process composition."""

from collections.abc import Mapping

from packages.application.secrets import SecretBackend, SecretBackendUnavailable
from packages.infrastructure.config import SecretBackendKind
from packages.infrastructure.secrets.env import EnvSecretBackend


def build_secret_backend(
    kind: SecretBackendKind,
    *,
    environment: Mapping[str, str] | None = None,
) -> SecretBackend:
    if kind is SecretBackendKind.ENV:
        return EnvSecretBackend(environment)
    raise SecretBackendUnavailable(
        "VaultSecretBackend is reserved but no Vault adapter is configured."
    )

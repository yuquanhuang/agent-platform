"""Environment Secret Backend contract tests."""

import pytest

from packages.application.secrets import (
    SecretBackendUnavailable,
    SecretReferenceInvalid,
)
from packages.infrastructure.config import SecretBackendKind
from packages.infrastructure.secrets import EnvSecretBackend, build_secret_backend


@pytest.mark.asyncio
async def test_env_backend_resolves_only_prefixed_reference() -> None:
    backend = EnvSecretBackend({"AP_SECRET_DATABASE_DSN": "postgres-secret"})

    secret = await backend.resolve("secret://env/AP_SECRET_DATABASE_DSN")

    assert secret.get_secret_value() == "postgres-secret"
    assert "postgres-secret" not in repr(secret)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reference",
    [
        "secret://env/DATABASE_DSN",
        "secret://local/database",
        "secret://env/AP_SECRET_../DATABASE",
        "secret://env/AP_SECRET_database",
    ],
)
async def test_env_backend_rejects_unsafe_reference(reference: str) -> None:
    with pytest.raises(SecretReferenceInvalid):
        await EnvSecretBackend({}).resolve(reference)


@pytest.mark.asyncio
async def test_env_backend_fails_closed_for_missing_or_invalid_value() -> None:
    backend = EnvSecretBackend(
        {
            "AP_SECRET_EMPTY": "",
            "AP_SECRET_NULL": "unsafe\x00value",
        }
    )

    with pytest.raises(SecretBackendUnavailable, match="not configured"):
        await backend.resolve("secret://env/AP_SECRET_MISSING")
    with pytest.raises(SecretBackendUnavailable, match="invalid"):
        await backend.resolve("secret://env/AP_SECRET_EMPTY")
    with pytest.raises(SecretBackendUnavailable, match="invalid"):
        await backend.resolve("secret://env/AP_SECRET_NULL")


def test_vault_mode_is_reserved_and_fails_closed() -> None:
    with pytest.raises(SecretBackendUnavailable, match="VaultSecretBackend"):
        build_secret_backend(SecretBackendKind.VAULT)

"""Secret resolution ports shared by deployment composition roots."""

from typing import Protocol

from pydantic import SecretStr


class SecretReferenceInvalid(ValueError):
    """A secret reference does not satisfy the configured backend contract."""


class SecretBackendUnavailable(RuntimeError):
    """The configured secret backend cannot resolve secrets safely."""


class SecretBackend(Protocol):
    async def resolve(self, reference: str) -> SecretStr:
        """Resolve one opaque reference without logging or persisting plaintext."""

        ...

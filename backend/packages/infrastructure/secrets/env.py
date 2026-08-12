"""Environment-variable Secret Backend for Kubernetes Secret injection."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from pydantic import SecretStr

from packages.application.secrets import (
    SecretBackendUnavailable,
    SecretReferenceInvalid,
)

_REFERENCE_PATTERN = re.compile(
    r"^secret://env/(?P<name>AP_SECRET_[A-Z][A-Z0-9_]{0,119})$"
)
_MAX_SECRET_LENGTH = 65_536


class EnvSecretBackend:
    """Resolve allowlisted AP_SECRET_* values injected into the process."""

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values = values if values is not None else os.environ

    async def resolve(self, reference: str) -> SecretStr:
        match = _REFERENCE_PATTERN.fullmatch(reference)
        if match is None:
            raise SecretReferenceInvalid(
                "Env secret references must use secret://env/AP_SECRET_*"
            )
        value = self._values.get(match.group("name"))
        if value is None:
            raise SecretBackendUnavailable(
                "The referenced environment secret is not configured."
            )
        if not value or len(value) > _MAX_SECRET_LENGTH or "\x00" in value:
            raise SecretBackendUnavailable(
                "The referenced environment secret is invalid."
            )
        return SecretStr(value)

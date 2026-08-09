"""Deterministic, non-reversible Run attempt fencing token issuance."""

import base64
import hashlib
import hmac
from uuid import UUID

from pydantic import SecretStr


class HmacFencingTokenIssuer:
    """Derive a stable token per attempt from deployment-managed secret material."""

    def __init__(self, secret: SecretStr) -> None:
        value = secret.get_secret_value().encode()
        if len(value) < 32:
            raise ValueError("Run fencing secret must contain at least 32 bytes")
        self._secret = value

    def issue(
        self, *, tenant_id: UUID, run_id: UUID, execution_attempt: int
    ) -> SecretStr:
        if execution_attempt < 1:
            raise ValueError("execution_attempt must be positive")
        message = f"run/{tenant_id}/{run_id}/{execution_attempt}".encode()
        digest = hmac.new(self._secret, message, hashlib.sha256).digest()
        token = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        return SecretStr(token)

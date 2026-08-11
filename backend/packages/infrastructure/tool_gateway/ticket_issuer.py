"""Deterministic, non-reversible Execution Ticket credential issuance."""

import base64
import hashlib
import hmac
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import SecretStr

from packages.application.tool_gateway import (
    ExecutionTicketCredential,
    execution_ticket_ref,
)


class HmacExecutionTicketIssuer:
    """Derive a stable nonce so retries never require plaintext persistence."""

    def __init__(self, secret: SecretStr) -> None:
        value = secret.get_secret_value().encode()
        if len(value) < 32:
            raise ValueError("Execution Ticket secret must contain at least 32 bytes")
        self._secret = value

    def issue(self, *, tenant_id: UUID, approval_id: UUID) -> ExecutionTicketCredential:
        ticket_id = uuid5(NAMESPACE_URL, f"execution-ticket/{tenant_id}/{approval_id}")
        message = f"execution-ticket/{tenant_id}/{approval_id}/{ticket_id}".encode()
        digest = hmac.new(self._secret, message, hashlib.sha256).digest()
        nonce = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        nonce_hash = f"sha256:{hashlib.sha256(nonce.encode()).hexdigest()}"
        return ExecutionTicketCredential(
            ticket_id=ticket_id,
            ticket_ref=execution_ticket_ref(ticket_id),
            nonce=SecretStr(nonce),
            nonce_hash=nonce_hash,
        )

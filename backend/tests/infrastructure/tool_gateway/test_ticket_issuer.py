"""Execution Ticket HMAC credential tests."""

from uuid import UUID

import pytest
from pydantic import SecretStr

from packages.infrastructure.tool_gateway import HmacExecutionTicketIssuer


def test_ticket_issuer_is_stable_bound_and_redacted() -> None:
    issuer = HmacExecutionTicketIssuer(SecretStr("s" * 32))
    tenant_id = UUID("11111111-1111-4111-8111-111111111111")
    approval_id = UUID("22222222-2222-4222-8222-222222222222")

    first = issuer.issue(tenant_id=tenant_id, approval_id=approval_id)
    replay = issuer.issue(tenant_id=tenant_id, approval_id=approval_id)
    other = issuer.issue(
        tenant_id=tenant_id,
        approval_id=UUID("33333333-3333-4333-8333-333333333333"),
    )

    assert first == replay
    assert first.ticket_id != other.ticket_id
    assert first.nonce_hash != other.nonce_hash
    assert first.nonce.get_secret_value() not in repr(first)


def test_ticket_issuer_rejects_short_secret() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        HmacExecutionTicketIssuer(SecretStr("short"))

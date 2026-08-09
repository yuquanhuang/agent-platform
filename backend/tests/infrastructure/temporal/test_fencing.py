"""Run attempt fencing token derivation tests."""

from uuid import UUID

from pydantic import SecretStr

from packages.infrastructure.temporal import HmacFencingTokenIssuer

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
RUN_ID = UUID("22222222-2222-4222-8222-222222222222")


def test_fencing_token_is_stable_per_attempt_and_not_exposed_by_repr() -> None:
    issuer = HmacFencingTokenIssuer(SecretStr("s" * 32))

    first = issuer.issue(tenant_id=TENANT_ID, run_id=RUN_ID, execution_attempt=1)
    replay = issuer.issue(tenant_id=TENANT_ID, run_id=RUN_ID, execution_attempt=1)
    next_attempt = issuer.issue(tenant_id=TENANT_ID, run_id=RUN_ID, execution_attempt=2)

    assert first.get_secret_value() == replay.get_secret_value()
    assert first.get_secret_value() != next_attempt.get_secret_value()
    assert first.get_secret_value() not in repr(first)

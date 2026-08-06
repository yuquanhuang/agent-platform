"""Server-controlled Mock OIDC adapter tests."""

from datetime import UTC, datetime

import pytest

from packages.contracts.public import PlatformError
from packages.infrastructure.auth.public import MockIdentityProvider
from packages.infrastructure.public import AppSettings

FIXED_TIME = datetime(2026, 8, 6, 1, 2, 3, tzinfo=UTC)


def test_mock_provider_emits_only_server_configured_claims() -> None:
    settings = AppSettings.model_validate(
        {
            "mock_external_subject": "server-subject",
            "mock_active_tenant_id": "11111111-1111-4111-8111-111111111111",
            "mock_membership_version": 7,
            "mock_platform_roles": ["platform_admin"],
        }
    )
    provider = MockIdentityProvider(settings, now=lambda: FIXED_TIME)

    principal = provider.authenticate("Bearer mock")

    assert principal.external_subject == "server-subject"
    assert principal.active_tenant_id == "11111111-1111-4111-8111-111111111111"
    assert principal.membership_version == 7
    assert principal.platform_roles == frozenset({"platform_admin"})
    assert principal.auth_time == FIXED_TIME


@pytest.mark.parametrize(
    "authorization", [None, "", "Basic mock", "Bearer wrong", "Bearer mock extra"]
)
def test_mock_provider_rejects_missing_or_invalid_bearer(
    authorization: str | None,
) -> None:
    provider = MockIdentityProvider(AppSettings.model_validate({}))

    with pytest.raises(PlatformError) as error:
        provider.authenticate(authorization)

    assert error.value.status_code == 401
    assert error.value.code == "UNAUTHENTICATED"

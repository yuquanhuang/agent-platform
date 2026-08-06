"""Server-controlled local/test Mock OIDC adapter."""

import hmac
from collections.abc import Callable
from datetime import UTC, datetime

from packages.contracts.public import (
    AuthenticatedPrincipal,
    IdentityProvider,
    unauthenticated,
)
from packages.infrastructure.config import AppSettings, AuthMode

MOCK_BEARER_TOKEN = "mock"


class MockIdentityProvider(IdentityProvider):
    """Accept one non-secret sentinel token and emit only server-side claims."""

    def __init__(
        self,
        settings: AppSettings,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if settings.auth_mode is not AuthMode.MOCK:
            raise ValueError("MockIdentityProvider requires AP_AUTH_MODE=mock")
        self._settings = settings
        self._now = now or (lambda: datetime.now(UTC))

    def authenticate(self, authorization: str | None) -> AuthenticatedPrincipal:
        if authorization is None:
            raise unauthenticated()
        scheme, separator, token = authorization.partition(" ")
        if (
            not separator
            or scheme.lower() != "bearer"
            or not hmac.compare_digest(token, MOCK_BEARER_TOKEN)
        ):
            raise unauthenticated("Bearer token is invalid.")

        return AuthenticatedPrincipal(
            identity_issuer=str(self._settings.mock_identity_issuer),
            external_subject=self._settings.mock_external_subject,
            display_name=self._settings.mock_display_name,
            email=self._settings.mock_email,
            platform_roles=frozenset(self._settings.mock_platform_roles),
            active_tenant_id=self._settings.mock_active_tenant_id,
            membership_version=self._settings.mock_membership_version,
            auth_time=self._now(),
        )

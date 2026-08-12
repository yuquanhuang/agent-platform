"""Authenticated HTTP cleanup adapter for the independent Sandbox Manager."""

from __future__ import annotations

from uuid import UUID

import httpx

from packages.application.sandbox import SandboxServiceAccess
from packages.contracts.public import (
    PlatformError,
    dependency_unavailable,
    permission_denied,
    resource_not_found,
    unauthenticated,
)
from packages.contracts.sandbox_api import SandboxActionResponse
from packages.infrastructure.auth.service_tokens import Ed25519ServiceTokenIssuer


class HttpSandboxCleanupController:
    """Destroy leaked sandboxes through the service-authenticated internal API."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        base_url: str,
        token_issuer: Ed25519ServiceTokenIssuer,
    ) -> None:
        normalized_base_url = base_url.rstrip("/")
        parsed = httpx.URL(normalized_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            raise ValueError("Sandbox Manager base URL must be HTTP(S)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "Sandbox Manager base URL cannot contain credentials or query"
            )
        self._client = client
        self._base_url = normalized_base_url
        self._token_issuer = token_issuer

    async def destroy(
        self,
        access: SandboxServiceAccess,
        *,
        sandbox_id: str,
    ) -> SandboxActionResponse:
        normalized_sandbox_id = str(UUID(sandbox_id))
        token = self._token_issuer.issue(access)
        try:
            response = await self._client.delete(
                f"{self._base_url}/internal/v1/sandboxes/{normalized_sandbox_id}",
                headers={
                    "Authorization": f"Bearer {token.get_secret_value()}",
                    "X-Request-ID": access.context.request_id,
                },
            )
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise dependency_unavailable("Sandbox Manager is unavailable.") from error

        if response.status_code == 401:
            raise unauthenticated("Sandbox Manager rejected the service identity.")
        if response.status_code == 403:
            raise permission_denied("Sandbox Manager rejected the cleanup permission.")
        if response.status_code == 404:
            raise resource_not_found(
                "Sandbox instance was not found by Sandbox Manager."
            )
        if response.status_code >= 500:
            raise dependency_unavailable("Sandbox Manager is unavailable.")
        if response.status_code != 200:
            raise PlatformError(
                status_code=409,
                code="RESOURCE_STATE_CONFLICT",
                message="Sandbox Manager rejected the cleanup request.",
            )
        try:
            result = SandboxActionResponse.model_validate(response.json())
        except ValueError as error:
            raise dependency_unavailable(
                "Sandbox Manager returned an invalid cleanup response."
            ) from error
        if result.sandbox_id != normalized_sandbox_id or result.action != "destroy":
            raise dependency_unavailable(
                "Sandbox Manager returned a mismatched cleanup response."
            )
        return result

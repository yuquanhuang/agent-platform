"""Explicit local/test-only Secret Reference resolver."""

from collections.abc import Mapping

from pydantic import SecretStr

from packages.contracts.public import TenantContext
from packages.domain.model_gateway import ProviderError


class MappingSecretReferenceResolver:
    """Resolve injected test values without persisting or logging them."""

    def __init__(self, values: Mapping[str, SecretStr]) -> None:
        self._values = dict(values)

    async def resolve(self, context: TenantContext, secret_ref: str) -> SecretStr:
        tenant_marker = f"secret://tenant/{context.tenant_id}/"
        if not secret_ref.startswith(tenant_marker):
            raise ProviderError(
                code="SECRET_SCOPE_MISMATCH",
                message="The provider credential reference is outside the tenant.",
                retryable=False,
                submission_state="not_submitted",
            )
        value = self._values.get(secret_ref)
        if value is None:
            raise ProviderError(
                code="SECRET_UNAVAILABLE",
                message="The provider credential is unavailable.",
                retryable=False,
                submission_state="not_submitted",
            )
        return value

"""Sandbox cleanup HTTP adapter tests."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr

from apps.sandbox_manager.app import create_sandbox_manager_app
from packages.application.sandbox import (
    SANDBOX_MANAGE_PERMISSION,
    SandboxInternalService,
    SandboxServiceAccess,
)
from packages.contracts.public import SubjectType, TenantContext
from packages.contracts.sandbox_api import SandboxActionResponse
from packages.infrastructure.auth.service_tokens import (
    Ed25519SandboxServiceIdentityProvider,
    Ed25519ServiceTokenIssuer,
)
from packages.infrastructure.sandbox import HttpSandboxCleanupController

TENANT_ID = "11111111-1111-4111-8111-111111111111"
SERVICE_ID = UUID("22222222-2222-4222-8222-222222222222")
SANDBOX_ID = "33333333-3333-4333-8333-333333333333"
NOW = datetime(2026, 8, 11, tzinfo=UTC)


class CleanupService:
    def __init__(self) -> None:
        self.tenant_ids: list[str] = []

    async def destroy(
        self, access: SandboxServiceAccess, *, sandbox_id: str
    ) -> SandboxActionResponse:
        self.tenant_ids.append(access.context.tenant_id)
        return SandboxActionResponse(
            sandbox_id=sandbox_id,
            action="destroy",
            status="TERMINATED",
            changed=True,
        )


def _key_pair() -> tuple[SecretStr, SecretStr]:
    private = Ed25519PrivateKey.generate()
    return (
        SecretStr(
            private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode()
        ),
        SecretStr(
            private.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        ),
    )


@pytest.mark.asyncio
async def test_cleanup_uses_signed_tenant_context_without_tenant_header() -> None:
    private_key, public_key = _key_pair()
    cleanup_service = CleanupService()
    identity = Ed25519SandboxServiceIdentityProvider(
        issuer="https://identity.agent-platform.test",
        audience="sandbox-manager",
        key_id="test-v1",
        public_key=public_key,
        allowed_subject_ids=(SERVICE_ID,),
        now=lambda: NOW,
    )
    application = create_sandbox_manager_app(
        identity_provider=identity,
        service=cast(SandboxInternalService, cleanup_service),
    )
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport) as client:
        controller = HttpSandboxCleanupController(
            client=client,
            base_url="http://sandbox-manager.test",
            token_issuer=Ed25519ServiceTokenIssuer(
                issuer="https://identity.agent-platform.test",
                audience="sandbox-manager",
                subject_id=SERVICE_ID,
                key_id="test-v1",
                private_key=private_key,
                now=lambda: NOW,
            ),
        )
        result = await controller.destroy(
            SandboxServiceAccess(
                context=TenantContext(
                    tenant_id=TENANT_ID,
                    subject_type=SubjectType.SERVICE,
                    subject_id=str(SERVICE_ID),
                    auth_time=NOW,
                    request_id="req_cleanup",
                    trace_id="trace_cleanup",
                ),
                permissions=frozenset({SANDBOX_MANAGE_PERMISSION}),
            ),
            sandbox_id=SANDBOX_ID,
        )

    assert result.status == "TERMINATED"
    assert cleanup_service.tenant_ids == [TENANT_ID]

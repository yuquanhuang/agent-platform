"""Internal Ed25519 workload token tests."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr
from starlette.requests import Request

from packages.application.sandbox import (
    SANDBOX_MANAGE_PERMISSION,
    SandboxServiceAccess,
)
from packages.contracts.public import PlatformError, SubjectType, TenantContext
from packages.infrastructure.auth.service_tokens import (
    Ed25519SandboxServiceIdentityProvider,
    Ed25519ServiceTokenIssuer,
)

TENANT_ID = "11111111-1111-4111-8111-111111111111"
SERVICE_ID = UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime(2026, 8, 11, tzinfo=UTC)


def _keys() -> tuple[SecretStr, SecretStr]:
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return SecretStr(private_pem), SecretStr(public_pem)


def _access(
    *, permissions: frozenset[str] = frozenset({SANDBOX_MANAGE_PERMISSION})
) -> SandboxServiceAccess:
    return SandboxServiceAccess(
        context=TenantContext(
            tenant_id=TENANT_ID,
            subject_type=SubjectType.SERVICE,
            subject_id=str(SERVICE_ID),
            auth_time=NOW,
            request_id="req_reconcile",
            trace_id="trace_reconcile",
        ),
        permissions=permissions,
    )


def _request(token: str, *, tenant_header: str | None = None) -> Request:
    headers = [(b"authorization", f"Bearer {token}".encode())]
    if tenant_header is not None:
        headers.append((b"x-tenant-id", tenant_header.encode()))
    request = Request({"type": "http", "method": "DELETE", "headers": headers})
    request.state.request_id = "req_verified"
    request.state.trace_id = "trace_verified"
    return request


@pytest.mark.asyncio
async def test_round_trip_derives_tenant_only_from_signed_claims() -> None:
    private_key, public_key = _keys()
    issuer = Ed25519ServiceTokenIssuer(
        issuer="https://identity.agent-platform.test",
        audience="sandbox-manager",
        subject_id=SERVICE_ID,
        key_id="test-v1",
        private_key=private_key,
        now=lambda: NOW,
    )
    verifier = Ed25519SandboxServiceIdentityProvider(
        issuer="https://identity.agent-platform.test",
        audience="sandbox-manager",
        key_id="test-v1",
        public_key=public_key,
        allowed_subject_ids=(SERVICE_ID,),
        now=lambda: NOW,
    )

    token = issuer.issue(_access()).get_secret_value()
    verified = await verifier.authenticate(
        _request(token, tenant_header="33333333-3333-4333-8333-333333333333")
    )

    assert verified.context.tenant_id == TENANT_ID
    assert verified.context.subject_id == str(SERVICE_ID)
    assert verified.context.request_id == "req_verified"
    assert verified.permissions == frozenset({SANDBOX_MANAGE_PERMISSION})


@pytest.mark.asyncio
async def test_verifier_rejects_wrong_audience_and_excessive_lifetime() -> None:
    private_key, public_key = _keys()
    private_value = private_key.get_secret_value()
    base_claims = {
        "iss": "https://identity.agent-platform.test",
        "sub": str(SERVICE_ID),
        "aud": "another-service",
        "tenant_id": TENANT_ID,
        "permissions": [SANDBOX_MANAGE_PERMISSION],
        "iat": int(NOW.timestamp()),
        "nbf": int(NOW.timestamp()),
        "exp": int((NOW + timedelta(seconds=120)).timestamp()),
        "jti": "a" * 32,
    }
    token = jwt.encode(
        base_claims,
        private_value,
        algorithm="EdDSA",
        headers={"kid": "test-v1", "typ": "JWT"},
    )
    verifier = Ed25519SandboxServiceIdentityProvider(
        issuer="https://identity.agent-platform.test",
        audience="sandbox-manager",
        key_id="test-v1",
        public_key=public_key,
        allowed_subject_ids=(SERVICE_ID,),
        max_ttl_seconds=60,
        now=lambda: NOW,
    )

    with pytest.raises(PlatformError, match="invalid") as error:
        await verifier.authenticate(_request(token))

    assert error.value.code == "UNAUTHENTICATED"


@pytest.mark.asyncio
async def test_valid_identity_without_permission_is_left_for_route_authorization() -> (
    None
):
    private_key, public_key = _keys()
    issuer = Ed25519ServiceTokenIssuer(
        issuer="https://identity.agent-platform.test",
        audience="sandbox-manager",
        subject_id=SERVICE_ID,
        key_id="test-v1",
        private_key=private_key,
        now=lambda: NOW,
    )
    with pytest.raises(PlatformError) as error:
        issuer.issue(_access(permissions=frozenset()))

    assert error.value.code == "PERMISSION_DENIED"

    claims: dict[str, object] = {
        "iss": "https://identity.agent-platform.test",
        "sub": str(SERVICE_ID),
        "aud": "sandbox-manager",
        "tenant_id": TENANT_ID,
        "permissions": [],
        "iat": int(NOW.timestamp()),
        "nbf": int(NOW.timestamp()),
        "exp": int((NOW + timedelta(seconds=60)).timestamp()),
        "jti": "b" * 32,
    }
    token = jwt.encode(
        claims,
        private_key.get_secret_value(),
        algorithm="EdDSA",
        headers={"kid": "test-v1", "typ": "JWT"},
    )
    verifier = Ed25519SandboxServiceIdentityProvider(
        issuer="https://identity.agent-platform.test",
        audience="sandbox-manager",
        key_id="test-v1",
        public_key=public_key,
        allowed_subject_ids=(SERVICE_ID,),
        now=lambda: NOW,
    )

    access = await verifier.authenticate(_request(token))
    with pytest.raises(PlatformError) as permission_error:
        access.authorize()
    assert permission_error.value.code == "PERMISSION_DENIED"

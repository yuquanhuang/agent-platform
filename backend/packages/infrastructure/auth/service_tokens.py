"""Short-lived Ed25519 service tokens for internal tenant-scoped calls."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import jwt
from fastapi import Request
from pydantic import SecretStr

from packages.application.sandbox import SandboxServiceAccess
from packages.contracts.public import SubjectType, TenantContext, unauthenticated

_JTI_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_ALGORITHM = "EdDSA"


class Ed25519ServiceTokenIssuer:
    """Mint bounded tokens from a private key held by the calling worker."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        subject_id: UUID,
        key_id: str,
        private_key: SecretStr,
        ttl_seconds: int = 60,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not issuer or not audience:
            raise ValueError("service token issuer and audience are required")
        if ttl_seconds < 30 or ttl_seconds > 300:
            raise ValueError("service token TTL must be between 30 and 300 seconds")
        if not key_id:
            raise ValueError("service token key id is required")
        self._issuer = issuer
        self._audience = audience
        self._subject_id = str(subject_id)
        self._key_id = key_id
        self._private_key = private_key.get_secret_value()
        self._ttl_seconds = ttl_seconds
        self._now = now or (lambda: datetime.now(UTC))

    def issue(self, access: SandboxServiceAccess) -> SecretStr:
        access.authorize()
        if access.context.subject_type is not SubjectType.SERVICE:
            raise ValueError("service token issuer requires a service context")
        if access.context.subject_id != self._subject_id:
            raise ValueError("service token context subject does not match issuer")
        issued_at = self._now()
        if issued_at.tzinfo is None or issued_at.utcoffset() != timedelta(0):
            raise ValueError("service token clock must return UTC")
        issued_timestamp = int(issued_at.timestamp())
        claims = {
            "iss": self._issuer,
            "sub": self._subject_id,
            "aud": self._audience,
            "tenant_id": access.context.tenant_id,
            "permissions": sorted(access.permissions),
            "iat": issued_timestamp,
            "nbf": issued_timestamp,
            "exp": issued_timestamp + self._ttl_seconds,
            "jti": uuid4().hex,
        }
        token = jwt.encode(
            claims,
            self._private_key,
            algorithm=_ALGORITHM,
            headers={"kid": self._key_id, "typ": "JWT"},
        )
        return SecretStr(token)


class Ed25519SandboxServiceIdentityProvider:
    """Verify internal service tokens and derive tenant context from claims."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        key_id: str,
        public_key: SecretStr,
        allowed_subject_ids: Iterable[UUID] = (),
        max_ttl_seconds: int = 60,
        clock_skew_seconds: int = 5,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not issuer or not audience or not key_id:
            raise ValueError("service token verification settings are required")
        if max_ttl_seconds < 30 or max_ttl_seconds > 300:
            raise ValueError("service token max TTL must be between 30 and 300 seconds")
        self._issuer = issuer
        self._audience = audience
        self._key_id = key_id
        self._public_key = public_key.get_secret_value()
        self._allowed_subject_ids = frozenset(
            str(value) for value in allowed_subject_ids
        )
        if not self._allowed_subject_ids:
            raise ValueError("at least one allowed service subject is required")
        self._max_ttl_seconds = max_ttl_seconds
        self._clock_skew_seconds = clock_skew_seconds
        self._now = now or (lambda: datetime.now(UTC))

    async def authenticate(self, request: Request) -> SandboxServiceAccess:
        authorization = request.headers.get("Authorization")
        if authorization is None:
            raise unauthenticated()
        scheme, separator, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not separator or not token or " " in token:
            raise unauthenticated("Internal service token is invalid.")
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != _ALGORITHM or header.get("typ") != "JWT":
                raise ValueError("unsupported token header")
            if header.get("kid") != self._key_id:
                raise ValueError("unknown token key")
            claims = jwt.decode(
                token,
                self._public_key,
                algorithms=[_ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                options={
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                    "require": [
                        "iss",
                        "sub",
                        "aud",
                        "tenant_id",
                        "permissions",
                        "iat",
                        "nbf",
                        "exp",
                        "jti",
                    ],
                },
            )
            subject_id = _uuid_claim(claims.get("sub"), "sub")
            tenant_id = _uuid_claim(claims.get("tenant_id"), "tenant_id")
            permissions = _permissions_claim(claims.get("permissions"))
            issued_at = _timestamp_claim(claims.get("iat"), "iat")
            not_before = _timestamp_claim(claims.get("nbf"), "nbf")
            expires_at = _timestamp_claim(claims.get("exp"), "exp")
            now = self._now()
            if now.tzinfo is None or now.utcoffset() != timedelta(0):
                raise ValueError("service token clock must return UTC")
            now_timestamp = int(now.timestamp())
            if (
                not_before != issued_at
                or expires_at <= issued_at
                or expires_at - issued_at > self._max_ttl_seconds
                or issued_at > now_timestamp + self._clock_skew_seconds
                or not_before > now_timestamp + self._clock_skew_seconds
                or expires_at + self._clock_skew_seconds <= now_timestamp
            ):
                raise ValueError("token lifetime is outside the configured bound")
            jti = claims.get("jti")
            if not isinstance(jti, str) or _JTI_PATTERN.fullmatch(jti) is None:
                raise ValueError("invalid token id")
            if subject_id not in self._allowed_subject_ids:
                raise ValueError("service subject is not allowed")
            request_id = _request_value(request, "request_id", "req_internal_service")
            trace_id = _request_value(request, "trace_id", "trace_internal_service")
            context = TenantContext(
                tenant_id=tenant_id,
                subject_type=SubjectType.SERVICE,
                subject_id=subject_id,
                auth_time=datetime.fromtimestamp(issued_at, tz=UTC),
                request_id=request_id,
                trace_id=trace_id,
            )
            return SandboxServiceAccess(
                context=context,
                permissions=frozenset(permissions),
            )
        except (jwt.InvalidTokenError, TypeError, ValueError) as error:
            raise unauthenticated("Internal service token is invalid.") from error


def _uuid_claim(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} claim must be a UUID")
    return str(UUID(value))


def _permissions_claim(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError("permissions claim is invalid")
    raw_permissions = cast(list[object], value)
    if len(raw_permissions) > 32:
        raise ValueError("permissions claim is invalid")
    permissions = tuple(
        item for item in raw_permissions if isinstance(item, str) and item
    )
    if len(permissions) != len(raw_permissions) or any(
        len(item) > 128 for item in permissions
    ):
        raise ValueError("permissions claim is invalid")
    return permissions


def _timestamp_claim(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} claim must be an integer timestamp")
    return value


def _request_value(request: Request, name: str, fallback: str) -> str:
    value = getattr(request.state, name, None)
    return value if isinstance(value, str) and value else fallback

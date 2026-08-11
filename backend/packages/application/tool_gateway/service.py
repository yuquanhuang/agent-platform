"""Fail-closed internal Tool Gateway with one-time ticket consumption."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import JsonValue, SecretStr

from packages.application.metadata import RequestMetadata
from packages.contracts.public import SubjectType, TenantContext
from packages.domain.execution_tickets import ExecutionTicketRecord

_TICKET_REF_PREFIX = "execution-ticket:"


@dataclass(frozen=True, slots=True)
class ExecutionTicketCredential:
    ticket_id: UUID
    ticket_ref: str
    nonce: SecretStr
    nonce_hash: str


@dataclass(frozen=True, slots=True)
class ExecutionTicketIssue:
    credential: ExecutionTicketCredential
    expires_at: datetime


class ExecutionTicketIssuer(Protocol):
    def issue(
        self, *, tenant_id: UUID, approval_id: UUID
    ) -> ExecutionTicketCredential: ...


@dataclass(frozen=True, slots=True)
class ToolExecutionRequest:
    tenant_id: UUID
    run_id: UUID
    execution_attempt: int
    approval_id: UUID
    requester_id: UUID
    deployment_id: UUID
    ticket_ref: str
    ticket_nonce: SecretStr
    tool_name: str
    tool_schema_hash: str
    parameter_digest: str
    policy_version: str
    arguments: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class ToolAuthorizationContext:
    permission_allowed: bool
    policy_allowed: bool
    tool_name: str
    tool_schema_hash: str
    policy_version: str
    deployment_id: UUID
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    status: Literal["SUCCEEDED", "FAILED"]
    output: JsonValue | None = None
    error_code: str | None = None


class ToolAuthorizationResolver(Protocol):
    async def resolve(
        self,
        context: TenantContext,
        *,
        request: ToolExecutionRequest,
    ) -> ToolAuthorizationContext: ...


class ExecutionTicketStore(Protocol):
    async def consume(
        self,
        context: TenantContext,
        *,
        request: ToolExecutionRequest,
        metadata: RequestMetadata,
        now: datetime,
    ) -> ExecutionTicketRecord: ...

    async def audit_tool_result(
        self,
        context: TenantContext,
        *,
        request: ToolExecutionRequest,
        metadata: RequestMetadata,
        result: ToolExecutionResult,
        now: datetime,
    ) -> None: ...

    async def audit_denied(
        self,
        context: TenantContext,
        *,
        request: ToolExecutionRequest,
        metadata: RequestMetadata,
        reason_codes: tuple[str, ...],
        now: datetime,
    ) -> None: ...


class ToolExecutionExecutor(Protocol):
    async def execute(
        self,
        context: TenantContext,
        *,
        request: ToolExecutionRequest,
    ) -> ToolExecutionResult: ...


class ToolExecutionDenied(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ToolGatewayService:
    """Re-authorize one exact call, consume first, then execute at most once."""

    def __init__(
        self,
        store: ExecutionTicketStore,
        authorization: ToolAuthorizationResolver,
        executor: ToolExecutionExecutor,
    ) -> None:
        self._store = store
        self._authorization = authorization
        self._executor = executor

    async def execute(
        self,
        context: TenantContext,
        *,
        request: ToolExecutionRequest,
        metadata: RequestMetadata,
        now: datetime | None = None,
    ) -> ToolExecutionResult:
        resolved_now = now or datetime.now(UTC)
        _validate_context(context, request)
        authorization = await self._authorization.resolve(context, request=request)
        reason_codes = _authorization_denials(authorization, request)
        if reason_codes:
            await self._store.audit_denied(
                context,
                request=request,
                metadata=metadata,
                reason_codes=reason_codes,
                now=resolved_now,
            )
            raise ToolExecutionDenied(
                reason_codes[0], "The tool invocation is no longer authorized."
            )
        await self._store.consume(
            context,
            request=request,
            metadata=metadata,
            now=resolved_now,
        )
        try:
            result = await self._executor.execute(context, request=request)
        except Exception:
            await self._store.audit_tool_result(
                context,
                request=request,
                metadata=metadata,
                result=ToolExecutionResult(
                    status="FAILED", error_code="TOOL_EXECUTION_FAILED"
                ),
                now=datetime.now(UTC),
            )
            raise
        await self._store.audit_tool_result(
            context,
            request=request,
            metadata=metadata,
            result=result,
            now=datetime.now(UTC),
        )
        return result


def execution_ticket_ref(ticket_id: UUID) -> str:
    return f"{_TICKET_REF_PREFIX}{ticket_id}"


def canonical_tool_parameter_digest(arguments: dict[str, JsonValue]) -> str:
    canonical = json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def parse_execution_ticket_ref(value: str) -> UUID:
    if not value.startswith(_TICKET_REF_PREFIX):
        raise ToolExecutionDenied(
            "EXECUTION_TICKET_REF_INVALID", "The Execution Ticket reference is invalid."
        )
    try:
        return UUID(value.removeprefix(_TICKET_REF_PREFIX))
    except ValueError as error:
        raise ToolExecutionDenied(
            "EXECUTION_TICKET_REF_INVALID", "The Execution Ticket reference is invalid."
        ) from error


def _validate_context(context: TenantContext, request: ToolExecutionRequest) -> None:
    if context.tenant_id != str(request.tenant_id):
        raise ToolExecutionDenied(
            "EXECUTION_TICKET_TENANT_MISMATCH",
            "The Execution Ticket cannot cross tenant boundaries.",
        )
    requester_matches = (
        context.subject_type is SubjectType.USER
        and context.subject_id == str(request.requester_id)
    )
    runtime_matches = (
        context.subject_type is SubjectType.SERVICE
        and context.subject_id == str(request.run_id)
    )
    if not requester_matches and not runtime_matches:
        raise ToolExecutionDenied(
            "EXECUTION_TICKET_REQUESTER_MISMATCH",
            "The Execution Ticket requester is invalid.",
        )
    if request.execution_attempt < 1:
        raise ToolExecutionDenied(
            "EXECUTION_TICKET_ATTEMPT_INVALID", "The execution attempt is invalid."
        )
    parse_execution_ticket_ref(request.ticket_ref)
    if canonical_tool_parameter_digest(request.arguments) != request.parameter_digest:
        raise ToolExecutionDenied(
            "EXECUTION_TICKET_PARAMETER_DIGEST_INVALID",
            "The tool arguments do not match their parameter digest.",
        )


def _authorization_denials(
    authorization: ToolAuthorizationContext,
    request: ToolExecutionRequest,
) -> tuple[str, ...]:
    reasons = list(authorization.reason_codes)
    if not authorization.permission_allowed:
        reasons.append("TOOL_PERMISSION_REVOKED")
    if not authorization.policy_allowed:
        reasons.append("TOOL_POLICY_REVOKED")
    if authorization.tool_name != request.tool_name:
        reasons.append("TOOL_IDENTITY_CHANGED")
    if authorization.tool_schema_hash != request.tool_schema_hash:
        reasons.append("TOOL_SCHEMA_CHANGED")
    if authorization.policy_version != request.policy_version:
        reasons.append("TOOL_POLICY_VERSION_CHANGED")
    if authorization.deployment_id != request.deployment_id:
        reasons.append("TOOL_DEPLOYMENT_CHANGED")
    return tuple(dict.fromkeys(reasons))

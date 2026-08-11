"""MCP configuration validation and isolated capability discovery."""

import asyncio
import hashlib
import ipaddress
import json
import re
from typing import Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from packages.application.outbox import PermanentOutboxError, WorkflowStartResult
from packages.contracts.generated.resource_content import ResourceContentMcp
from packages.contracts.public import SubjectType, TenantContext
from packages.domain.outbox import OutboxEvent
from packages.domain.public import (
    McpCapabilityEvidenceRecord,
    McpDiscoveredTool,
    McpDiscoveryResponse,
    McpDiscoveryResult,
    McpDiscoveryTarget,
    McpToolRiskLevel,
)

MCP_CAPABILITY_DISCOVERY_EVENT = "mcp.capability_discovery_requested"
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_SECRET_PLACEHOLDER = re.compile(r"\$\{(secret://[^{}\s]+)\}")
_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,254}$")
_PROTOCOL_VERSION = re.compile(r"^20[0-9]{2}-[0-9]{2}-[0-9]{2}$")
_BLOCKED_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "cookie",
        "forwarded",
        "host",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
    }
)
_SENSITIVE_HEADER_PARTS = ("authorization", "api-key", "apikey", "secret", "token")
_MAX_DISCOVERY_BYTES = 2 * 1024 * 1024


class McpCapabilityDiscoverer(Protocol):
    """Call an MCP server through a dedicated Gateway, never the API process."""

    async def discover(
        self, context: TenantContext, target: McpDiscoveryTarget
    ) -> McpDiscoveryResponse: ...


class McpDiscoveryCompletionStore(Protocol):
    async def mark_running(
        self, context: TenantContext, operation_id: UUID
    ) -> bool: ...

    async def record_terminal(
        self,
        context: TenantContext,
        *,
        operation_id: UUID,
        target: McpDiscoveryTarget,
        result: McpDiscoveryResult,
        discovered_by: UUID,
        error: dict[str, object] | None,
    ) -> McpCapabilityEvidenceRecord: ...


class McpDiscoveryEvidenceStore(Protocol):
    async def get_publishable_evidence(
        self,
        context: TenantContext,
        *,
        definition_id: UUID,
        draft_resource_version: int,
        content_hash: str,
        allowed_tools: tuple[str, ...],
    ) -> McpCapabilityEvidenceRecord | None: ...

    async def get_published_evidence(
        self,
        context: TenantContext,
        *,
        source_version_id: UUID,
        definition_id: UUID,
        content_hash: str,
        allowed_tools: tuple[str, ...],
    ) -> McpCapabilityEvidenceRecord | None: ...


class McpDiscoveryError(RuntimeError):
    """Safe failure returned by a production MCP Gateway adapter."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.safe_message = message
        super().__init__(message)


class McpDiscoveryPayloadV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    requested_by: UUID
    definition_id: UUID
    draft_resource_version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    transport: str
    endpoint: str = Field(max_length=2048)
    header_templates: dict[str, str] = Field(max_length=50)
    secret_refs: list[str] = Field(max_length=20)
    timeout_seconds: int = Field(ge=1, le=300)
    allowed_tools: list[str] = Field(max_length=500)


def validate_mcp_content(
    context: TenantContext, content: ResourceContentMcp, *, publishing: bool = False
) -> None:
    """Validate SSRF, Header and Secret Reference boundaries."""

    if content.transport != "streamable_http":
        raise ValueError("Only streamable_http MCP is frozen in R10.")
    parsed = urlsplit(content.endpoint)
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "MCP endpoint must be an HTTPS URL without credentials or query data."
        )
    normalized_host = hostname.rstrip(".").lower()
    if normalized_host != hostname or normalized_host in {"localhost", "metadata"}:
        raise ValueError("MCP endpoint hostname is not allowed.")
    if normalized_host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("MCP endpoint must not target a local hostname.")
    try:
        ipaddress.ip_address(normalized_host.strip("[]"))
    except ValueError:
        pass
    else:
        raise ValueError(
            "MCP endpoint must use an approved DNS hostname, not an IP literal."
        )

    secret_refs = tuple(content.secret_refs)
    if len(secret_refs) != len(set(secret_refs)):
        raise ValueError("MCP secret_refs must be unique.")
    expected_prefix = f"secret://tenant/{context.tenant_id}/"
    if any(not ref.startswith(expected_prefix) for ref in secret_refs):
        raise ValueError("MCP Secret References must belong to the current tenant.")

    referenced: set[str] = set()
    for name, template in (content.header_templates or {}).items():
        normalized_name = name.lower()
        if not _HEADER_NAME.fullmatch(name) or normalized_name in _BLOCKED_HEADERS:
            raise ValueError("MCP header template contains a forbidden header name.")
        if "\r" in template or "\n" in template or len(template) > 1000:
            raise ValueError("MCP header template contains invalid characters.")
        placeholders = set(_SECRET_PLACEHOLDER.findall(template))
        if "secret://" in template and not placeholders:
            raise ValueError(
                "MCP Secret References must use ${secret://...} placeholders."
            )
        if any(ref not in secret_refs for ref in placeholders):
            raise ValueError("MCP header template references an undeclared Secret.")
        if (
            any(part in normalized_name for part in _SENSITIVE_HEADER_PARTS)
            and not placeholders
        ):
            raise ValueError(
                "Sensitive MCP headers must use a Secret Reference placeholder."
            )
        referenced.update(placeholders)
    if set(secret_refs) != referenced:
        raise ValueError(
            "Every MCP Secret Reference must be used by a header template."
        )

    allowed_tools = content.allowed_tools
    if publishing and allowed_tools is None:
        raise ValueError("MCP publication requires an explicit allowed_tools list.")
    if allowed_tools is not None:
        if len(allowed_tools) != len(set(allowed_tools)):
            raise ValueError("MCP allowed_tools must be unique.")
        if any(_TOOL_NAME.fullmatch(name) is None for name in allowed_tools):
            raise ValueError("MCP allowed_tools contains an invalid tool name.")


def validate_discovery_response(
    target: McpDiscoveryTarget, response: McpDiscoveryResponse
) -> McpDiscoveryResult:
    """Normalize untrusted MCP capability data into deterministic evidence."""

    findings: list[dict[str, JsonValue]] = []
    if _PROTOCOL_VERSION.fullmatch(response.protocol_version) is None:
        findings.append(_finding("UNSUPPORTED_MCP_PROTOCOL", "/protocol_version"))
    if not response.server_name or len(response.server_name) > 128:
        findings.append(_finding("INVALID_MCP_SERVER_IDENTITY", "/server/name"))
    if response.server_version is not None and len(response.server_version) > 64:
        findings.append(_finding("INVALID_MCP_SERVER_IDENTITY", "/server/version"))
    if len(response.tools) > 500:
        findings.append(_finding("MCP_TOOL_LIMIT_EXCEEDED", "/tools"))

    tools: list[McpDiscoveredTool] = []
    names: set[str] = set()
    for index, raw in enumerate(response.tools[:500]):
        path = f"/tools/{index}"
        if _TOOL_NAME.fullmatch(raw.name) is None or raw.name in names:
            findings.append(_finding("INVALID_OR_DUPLICATE_MCP_TOOL", path))
            continue
        names.add(raw.name)
        if raw.description is not None and len(raw.description) > 2000:
            findings.append(_finding("MCP_TOOL_DESCRIPTION_TOO_LARGE", path))
            continue
        if not _valid_schema(raw.input_schema) or (
            raw.output_schema is not None and not _valid_schema(raw.output_schema)
        ):
            findings.append(_finding("INVALID_MCP_TOOL_SCHEMA", path))
            continue
        try:
            risk = _tool_risk(raw.annotations)
        except ValueError:
            findings.append(_finding("INVALID_MCP_TOOL_ANNOTATIONS", path))
            continue
        schema_payload: dict[str, JsonValue] = {
            "name": raw.name,
            "input_schema": raw.input_schema,
            "output_schema": raw.output_schema,
        }
        tools.append(
            McpDiscoveredTool(
                name=raw.name,
                description=raw.description,
                input_schema=raw.input_schema,
                output_schema=raw.output_schema,
                schema_hash=_hash_json(schema_payload),
                risk_level=risk,
            )
        )
    missing = sorted(set(target.allowed_tools) - {tool.name for tool in tools})
    if missing:
        findings.append(_finding("MCP_ALLOWED_TOOL_NOT_DISCOVERED", "/allowed_tools"))
    if len(_canonical_json(_tools_json(tools))) > _MAX_DISCOVERY_BYTES:
        findings.append(_finding("MCP_CAPABILITY_PAYLOAD_TOO_LARGE", "/tools"))
    capability_payload: dict[str, JsonValue] = {
        "protocol_version": response.protocol_version,
        "server_name": response.server_name,
        "server_version": response.server_version,
        "allowed_tools": list(target.allowed_tools),
        "tools": _tools_json(tools),
    }
    capability_hash = _hash_json(capability_payload)
    return McpDiscoveryResult(
        status="REJECTED" if findings else "PASSED",
        protocol_version=response.protocol_version,
        server_name=response.server_name,
        server_version=response.server_version,
        tools=tuple(tools),
        capability_hash=capability_hash,
        findings=tuple(findings),
    )


class McpCapabilityDiscoveryHandler:
    """Run MCP discovery outside the API process and terminalize its Operation."""

    def __init__(
        self,
        *,
        discoverer: McpCapabilityDiscoverer,
        store: McpDiscoveryCompletionStore,
    ) -> None:
        self._discoverer = discoverer
        self._store = store

    async def start(self, event: OutboxEvent) -> WorkflowStartResult:
        if event.event_type != MCP_CAPABILITY_DISCOVERY_EVENT:
            raise PermanentOutboxError(f"unsupported event_type: {event.event_type}")
        if event.payload_schema_version != 1:
            raise PermanentOutboxError("unsupported MCP discovery payload version")
        try:
            payload = McpDiscoveryPayloadV1.model_validate(event.payload)
            target = _target(payload)
        except ValueError as exc:
            raise PermanentOutboxError("invalid MCP discovery payload") from exc
        context = TenantContext(
            tenant_id=str(event.tenant_id),
            subject_type=SubjectType.SERVICE,
            subject_id=str(event.aggregate_id),
            auth_time=event.created_at,
            request_id=f"outbox:{event.id}",
            trace_id=f"outbox:{event.id}",
        )
        should_discover = await self._store.mark_running(context, payload.operation_id)
        if not should_discover:
            return WorkflowStartResult(
                workflow_id=f"mcp-capability-discovery/{payload.operation_id}",
                run_id=None,
                already_exists=True,
            )
        error: dict[str, object] | None = None
        try:
            response = await asyncio.wait_for(
                self._discoverer.discover(context, target),
                timeout=target.timeout_seconds,
            )
            result = validate_discovery_response(target, response)
            if result.status == "REJECTED":
                error = {
                    "code": "MCP_CAPABILITY_REJECTED",
                    "message": "MCP capability discovery failed security validation.",
                }
        except asyncio.CancelledError:
            raise
        except McpDiscoveryError as exc:
            error = {"code": exc.code, "message": exc.safe_message}
            result = _failed_result(exc.code)
        except TimeoutError:
            error = {
                "code": "MCP_DISCOVERY_TIMEOUT",
                "message": "MCP capability discovery timed out.",
            }
            result = _failed_result("MCP_DISCOVERY_TIMEOUT")
        except (OSError, RuntimeError):
            error = {
                "code": "MCP_DISCOVERY_UNAVAILABLE",
                "message": "MCP capability discovery is unavailable.",
            }
            result = _failed_result("MCP_DISCOVERY_UNAVAILABLE")
        await self._store.record_terminal(
            context,
            operation_id=payload.operation_id,
            target=target,
            result=result,
            discovered_by=payload.requested_by,
            error=error,
        )
        return WorkflowStartResult(
            workflow_id=f"mcp-capability-discovery/{payload.operation_id}",
            run_id=None,
            already_exists=False,
        )


def _target(payload: McpDiscoveryPayloadV1) -> McpDiscoveryTarget:
    if payload.transport != "streamable_http":
        raise ValueError("unsupported MCP transport")
    return McpDiscoveryTarget(
        definition_id=payload.definition_id,
        draft_resource_version=payload.draft_resource_version,
        content_hash=payload.content_hash,
        transport="streamable_http",
        endpoint=payload.endpoint,
        header_templates=payload.header_templates,
        secret_refs=tuple(payload.secret_refs),
        timeout_seconds=payload.timeout_seconds,
        allowed_tools=tuple(payload.allowed_tools),
    )


def _valid_schema(value: JsonValue) -> bool:
    if not isinstance(value, (bool, dict)):
        return False
    try:
        Draft202012Validator.check_schema(cast(bool | dict[str, object], value))
    except SchemaError:
        return False
    return True


def _tool_risk(annotations: dict[str, JsonValue] | None) -> McpToolRiskLevel:
    values = annotations or {}
    for key in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
        if key in values and not isinstance(values[key], bool):
            raise ValueError("MCP tool annotations must be booleans")
    if values.get("destructiveHint") is True or values.get("openWorldHint") is True:
        return "HIGH"
    return "MEDIUM"


def _tools_json(tools: list[McpDiscoveredTool]) -> list[JsonValue]:
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "output_schema": tool.output_schema,
            "schema_hash": tool.schema_hash,
            "risk_level": tool.risk_level,
        }
        for tool in sorted(tools, key=lambda item: item.name)
    ]


def _finding(code: str, path: str) -> dict[str, JsonValue]:
    return {"code": code, "severity": "HIGH", "path": path, "blocking": True}


def _failed_result(code: str) -> McpDiscoveryResult:
    return McpDiscoveryResult(
        status="FAILED",
        protocol_version=None,
        server_name=None,
        server_version=None,
        tools=(),
        capability_hash=None,
        findings=(_finding(code, "/"),),
    )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()


def _hash_json(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value)).hexdigest()}"

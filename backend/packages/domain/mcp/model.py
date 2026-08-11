"""Immutable MCP configuration and capability discovery facts."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import JsonValue

McpDiscoveryStatus = Literal["PASSED", "REJECTED", "FAILED"]
McpToolRiskLevel = Literal["MEDIUM", "HIGH", "CRITICAL"]


@dataclass(frozen=True, slots=True)
class McpDiscoveryTarget:
    """Secret-reference-only target sent to the isolated MCP Gateway."""

    definition_id: UUID
    draft_resource_version: int
    content_hash: str
    transport: Literal["streamable_http"]
    endpoint: str
    header_templates: dict[str, str]
    secret_refs: tuple[str, ...]
    timeout_seconds: int
    allowed_tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class McpRawTool:
    """Untrusted tool description returned by an MCP server."""

    name: str
    description: str | None
    input_schema: JsonValue
    output_schema: JsonValue | None = None
    annotations: dict[str, JsonValue] | None = None


@dataclass(frozen=True, slots=True)
class McpDiscoveryResponse:
    """Untrusted initialize/tools-list response returned by the Gateway."""

    protocol_version: str
    server_name: str
    server_version: str | None
    tools: tuple[McpRawTool, ...]


@dataclass(frozen=True, slots=True)
class McpDiscoveredTool:
    """Validated immutable tool capability."""

    name: str
    description: str | None
    input_schema: JsonValue
    output_schema: JsonValue | None
    schema_hash: str
    risk_level: McpToolRiskLevel


@dataclass(frozen=True, slots=True)
class McpDiscoveryResult:
    """Terminal discovery result persisted before publication."""

    status: McpDiscoveryStatus
    protocol_version: str | None
    server_name: str | None
    server_version: str | None
    tools: tuple[McpDiscoveredTool, ...]
    capability_hash: str | None
    findings: tuple[dict[str, JsonValue], ...]


@dataclass(frozen=True, slots=True)
class McpCapabilityEvidenceRecord:
    """One immutable discovery evidence row that may bind one version."""

    id: UUID
    tenant_id: UUID
    definition_id: UUID
    operation_id: UUID | None
    draft_resource_version: int
    content_hash: str
    status: McpDiscoveryStatus
    capability_hash: str | None
    tool_names: tuple[str, ...]
    published_version_id: UUID | None
    discovered_at: datetime
    discovered_by: UUID


@dataclass(frozen=True, slots=True)
class McpCapabilitySnapshotInput:
    """Published MCP capability evidence consumed by Bundle compilation."""

    id: UUID
    tenant_id: UUID
    definition_id: UUID
    published_version_id: UUID
    content_hash: str
    capability_hash: str
    protocol_version: str
    server_name: str
    server_version: str | None
    allowed_tools: tuple[str, ...]
    tools: tuple[McpDiscoveredTool, ...]

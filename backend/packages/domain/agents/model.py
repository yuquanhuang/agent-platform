"""Agent Draft records shared by application and persistence layers."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import JsonValue

AgentRuntimeType = Literal["agentscope", "codex"]
AgentVisibility = Literal["private", "tenant"]
AgentStatus = Literal["DRAFT", "ACTIVE", "DISABLED", "DELETING", "DELETED"]
BindingVersionPolicy = Literal["fixed", "resolve_on_publish"]
BindingRole = Literal["primary", "fallback_1", "fallback_2"]


@dataclass(frozen=True, slots=True)
class AgentReferenceRecord:
    resource_type: str
    resource_id: UUID
    reference_type: Literal[
        "deployment", "schedule", "child_agent", "session", "mcp_entry"
    ]


@dataclass(frozen=True, slots=True)
class AgentBindingRecord:
    resource_type: str
    resource_id: UUID
    version_policy: BindingVersionPolicy
    version_id: UUID | None
    binding_role: BindingRole | None
    configuration_schema_version: Literal["model-routing/v1"] | None
    configuration: dict[str, JsonValue] | None


@dataclass(frozen=True, slots=True)
class AgentRecord:
    id: UUID
    tenant_id: UUID
    code: str
    name: str
    description: str | None
    runtime_type: AgentRuntimeType
    visibility: AgentVisibility
    tags: tuple[str, ...]
    bindings: tuple[AgentBindingRecord, ...]
    status: AgentStatus
    resource_version: int
    active_deployment_id: UUID | None
    owner_user_id: UUID
    created_at: datetime
    updated_at: datetime

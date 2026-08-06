"""Versioned resource registry records and canonical content helpers."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from pydantic import JsonValue, TypeAdapter

from packages.contracts.generated.resource_content import (
    ResourceContentMcp,
    ResourceContentModelConfig,
    ResourceContentModelProvider,
    ResourceContentPrompt,
    ResourceContentRuntimeTarget,
    ResourceContentSandboxProfile,
    ResourceContentSkill,
)

ResourceType = Literal[
    "prompt",
    "skill",
    "mcp",
    "model_provider",
    "model_config",
    "runtime_target",
    "sandbox_profile",
]
ResourceVisibility = Literal["private", "tenant"]
ResourceRegistryStatus = Literal["DRAFT", "ACTIVE", "DISABLED", "DELETING", "DELETED"]
ResourceVersionStatus = Literal["PUBLISHED", "DISABLED"]
ResourceReferenceType = Literal[
    "draft_binding", "snapshot", "deployment", "schedule", "session"
]

RESOURCE_TYPES: frozenset[str] = frozenset(
    {
        "prompt",
        "skill",
        "mcp",
        "model_provider",
        "model_config",
        "runtime_target",
        "sandbox_profile",
    }
)
type ResourceContentValue = (
    ResourceContentPrompt
    | ResourceContentSkill
    | ResourceContentMcp
    | ResourceContentModelProvider
    | ResourceContentModelConfig
    | ResourceContentRuntimeTarget
    | ResourceContentSandboxProfile
)

RESOURCE_CONTENT_ADAPTER: TypeAdapter[ResourceContentValue] = TypeAdapter(
    ResourceContentValue
)


@dataclass(frozen=True, slots=True)
class ResourceDefinitionRecord:
    id: UUID
    tenant_id: UUID
    resource_type: ResourceType
    code: str
    name: str
    description: str | None
    owner_user_id: UUID
    visibility: ResourceVisibility
    content_schema_version: str
    content: ResourceContentValue
    status: ResourceRegistryStatus
    resource_version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ResourceVersionRecord:
    id: UUID
    tenant_id: UUID
    definition_id: UUID
    version_no: int
    schema_version: str
    content: ResourceContentValue
    content_hash: str
    release_note: str | None
    status: ResourceVersionStatus
    published_at: datetime
    published_by: UUID


@dataclass(frozen=True, slots=True, order=True)
class ResourceReferenceRecord:
    resource_type: str
    resource_id: UUID
    reference_type: ResourceReferenceType
    version_id: UUID | None = None


def parse_resource_content(payload: object) -> ResourceContentValue:
    """Validate persisted JSON against the frozen ResourceContent union."""

    return RESOURCE_CONTENT_ADAPTER.validate_python(payload)


def resource_content_json(content: ResourceContentValue) -> dict[str, JsonValue]:
    """Return the stable JSON representation used for persistence and hashing."""

    payload = content.model_dump(mode="json", by_alias=True, exclude_unset=True)
    return cast(dict[str, JsonValue], payload)


def validate_content_type(
    resource_type: ResourceType, content: ResourceContentValue
) -> None:
    if content.resource_type != resource_type:
        raise ValueError("content.resource_type must match the resource collection")


def canonical_content_hash(content: ResourceContentValue) -> str:
    payload = json.dumps(
        resource_content_json(content),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"

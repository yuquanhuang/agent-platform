"""Deterministic Agent Snapshot compiler."""

import hashlib
import json
from typing import cast

from pydantic import JsonValue

from packages.domain.publishing.model import (
    CompiledAgentSnapshot,
    ResolvedSnapshotBinding,
    SnapshotCompilationInput,
)

SNAPSHOT_SCHEMA_VERSION = "agent-snapshot/v1"
SNAPSHOT_COMPILER_VERSION = "agent-snapshot-compiler/1"
MODEL_ROLE_ORDER = {"primary": 0, "fallback_1": 1, "fallback_2": 2}


def compile_agent_snapshot(
    compilation_input: SnapshotCompilationInput,
) -> CompiledAgentSnapshot:
    """Compile canonical content without time, randomness, or mutable Draft reads."""

    ordered_bindings = tuple(sorted(compilation_input.bindings, key=_binding_sort_key))
    bindings = [_binding_payload(binding) for binding in ordered_bindings]
    content = cast(
        dict[str, JsonValue],
        {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "agent": {
                "id": str(compilation_input.agent_id),
                "code": compilation_input.code,
                "name": compilation_input.name,
                "description": compilation_input.description,
                "runtime_type": compilation_input.runtime_type,
                "visibility": compilation_input.visibility,
                "tags": list(compilation_input.tags),
                "default_language": compilation_input.default_language,
            },
            "source": {
                "draft_resource_version": compilation_input.draft_resource_version,
            },
            "bindings": bindings,
        },
    )
    model_routing = _model_routing_payload(ordered_bindings)
    if model_routing is not None:
        content["model_routing"] = model_routing

    compiler_input = {
        "compiler_version": SNAPSHOT_COMPILER_VERSION,
        "agent_id": str(compilation_input.agent_id),
        "draft_resource_version": compilation_input.draft_resource_version,
        "resolved_bindings": [
            {
                "resource_type": binding.resource_type,
                "resource_id": str(binding.resource_id),
                "version_id": str(binding.version_id),
                "content_hash": binding.content_hash,
                "model_binding_snapshot_hash": binding.model_binding_snapshot_hash,
                "agent_snapshot_id": (
                    str(binding.agent_snapshot_id)
                    if binding.agent_snapshot_id is not None
                    else None
                ),
            }
            for binding in ordered_bindings
        ],
    }
    return CompiledAgentSnapshot(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        content=content,
        content_hash=_canonical_hash(content),
        compiler_input_hash=_canonical_hash(compiler_input),
    )


def _binding_payload(binding: ResolvedSnapshotBinding) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "resource_type": binding.resource_type,
        "resource_id": str(binding.resource_id),
        "version_id": str(binding.version_id),
        "version_no": binding.version_no,
        "schema_version": binding.schema_version,
        "content_hash": binding.content_hash,
    }
    if binding.binding_role is not None:
        payload["binding_role"] = binding.binding_role
    if binding.configuration_schema_version is not None:
        payload["configuration_schema_version"] = binding.configuration_schema_version
    if binding.configuration is not None:
        payload["configuration"] = binding.configuration
    if binding.model_binding_snapshot_id is not None:
        payload["model_binding_snapshot"] = {
            "id": str(binding.model_binding_snapshot_id),
            "content_hash": binding.model_binding_snapshot_hash,
        }
    if binding.agent_snapshot_id is not None:
        payload["agent_snapshot_id"] = str(binding.agent_snapshot_id)
    return payload


def _model_routing_payload(
    bindings: tuple[ResolvedSnapshotBinding, ...],
) -> dict[str, JsonValue] | None:
    model_bindings = [
        binding for binding in bindings if binding.resource_type == "model"
    ]
    if not model_bindings:
        return None
    if any(
        binding.model_binding_snapshot_id is None
        or binding.model_binding_snapshot_hash is None
        or binding.binding_role not in MODEL_ROLE_ORDER
        for binding in model_bindings
    ):
        raise ValueError("Model routes require immutable binding snapshots and roles")
    primary = next(
        (binding for binding in model_bindings if binding.binding_role == "primary"),
        None,
    )
    fallback_error_codes: list[JsonValue] = []
    if primary is not None and primary.configuration is not None:
        raw_codes = primary.configuration.get("fallback_error_codes", [])
        if isinstance(raw_codes, list):
            fallback_error_codes = cast(list[JsonValue], raw_codes)
    return {
        "schema_version": "model-routing/v1",
        "fallback_error_codes": fallback_error_codes,
        "routes": [
            {
                "role": binding.binding_role,
                "model_config_version_id": str(binding.version_id),
                "model_binding_snapshot_id": str(binding.model_binding_snapshot_id),
                "model_binding_snapshot_hash": binding.model_binding_snapshot_hash,
            }
            for binding in model_bindings
        ],
    }


def _binding_sort_key(binding: ResolvedSnapshotBinding) -> tuple[int, str, str]:
    if binding.resource_type == "model":
        return (
            MODEL_ROLE_ORDER.get(binding.binding_role or "", 99),
            binding.resource_type,
            str(binding.resource_id),
        )
    return (10, binding.resource_type, str(binding.resource_id))


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

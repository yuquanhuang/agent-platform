"""Deterministic Agent Snapshot compiler tests."""

from typing import cast
from uuid import UUID

from packages.domain.public import (
    ResolvedSnapshotBinding,
    SnapshotCompilationInput,
    compile_agent_snapshot,
)

AGENT_ID = UUID("11111111-1111-4111-8111-111111111111")
MODEL_ID = UUID("22222222-2222-4222-8222-222222222222")
MODEL_VERSION_ID = UUID("33333333-3333-4333-8333-333333333333")
MODEL_SNAPSHOT_ID = UUID("44444444-4444-4444-8444-444444444444")
PROMPT_ID = UUID("55555555-5555-4555-8555-555555555555")
PROMPT_VERSION_ID = UUID("66666666-6666-4666-8666-666666666666")


def compilation_input(*, draft_version: int = 3) -> SnapshotCompilationInput:
    return SnapshotCompilationInput(
        agent_id=AGENT_ID,
        code="support_agent",
        name="Support Agent",
        description="Answers support questions",
        runtime_type="agentscope",
        visibility="tenant",
        tags=("support", "internal"),
        default_language="zh-CN",
        draft_resource_version=draft_version,
        bindings=(
            ResolvedSnapshotBinding(
                resource_type="prompt",
                resource_id=PROMPT_ID,
                version_id=PROMPT_VERSION_ID,
                version_no=2,
                schema_version="1.0",
                content_hash="sha256:" + "a" * 64,
            ),
            ResolvedSnapshotBinding(
                resource_type="model",
                resource_id=MODEL_ID,
                version_id=MODEL_VERSION_ID,
                version_no=1,
                schema_version="1.0",
                content_hash="sha256:" + "b" * 64,
                binding_role="primary",
                configuration_schema_version="model-routing/v1",
                configuration={
                    "fallback_error_codes": [
                        "RATE_LIMITED",
                        "PROVIDER_UNAVAILABLE",
                    ]
                },
                model_binding_snapshot_id=MODEL_SNAPSHOT_ID,
                model_binding_snapshot_hash="sha256:" + "c" * 64,
            ),
        ),
    )


def test_compiler_is_canonical_and_freezes_model_routing() -> None:
    first = compile_agent_snapshot(compilation_input())
    second = compile_agent_snapshot(compilation_input())

    assert first == second
    assert first.content_hash.startswith("sha256:")
    assert first.compiler_input_hash.startswith("sha256:")
    bindings = cast(list[dict[str, object]], first.content["bindings"])
    assert [item["resource_type"] for item in bindings] == [
        "model",
        "prompt",
    ]
    assert first.content["model_routing"] == {
        "schema_version": "model-routing/v1",
        "fallback_error_codes": ["RATE_LIMITED", "PROVIDER_UNAVAILABLE"],
        "routes": [
            {
                "role": "primary",
                "model_config_version_id": str(MODEL_VERSION_ID),
                "model_binding_snapshot_id": str(MODEL_SNAPSHOT_ID),
                "model_binding_snapshot_hash": "sha256:" + "c" * 64,
            }
        ],
    }


def test_draft_version_changes_compiler_input_and_content_hashes() -> None:
    first = compile_agent_snapshot(compilation_input(draft_version=3))
    second = compile_agent_snapshot(compilation_input(draft_version=4))

    assert first.compiler_input_hash != second.compiler_input_hash
    assert first.content_hash != second.content_hash

"""Deterministic and redacted canonical Agent Snapshot diff."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import cast

from pydantic import JsonValue

from packages.domain.publishing.model import (
    SnapshotChangeRecord,
    SnapshotDiffCategory,
)

_MISSING = object()
_SENSITIVE_SEGMENTS = frozenset(
    {"secret", "secrets", "secret_ref", "secret_refs", "credential", "token"}
)
_BODY_SEGMENTS = frozenset(
    {"prompt", "template", "instructions", "system_prompt", "content_json"}
)


def diff_agent_snapshot_content(
    before: Mapping[str, object] | None,
    after: Mapping[str, object],
) -> tuple[SnapshotChangeRecord, ...]:
    """Compare canonical content without returning mutable or sensitive bodies."""

    changes: list[SnapshotChangeRecord] = []
    _walk_diff(before if before is not None else _MISSING, after, "", None, changes)
    return tuple(sorted(changes, key=lambda item: (item.path, item.change_type)))


def _walk_diff(
    before: object,
    after: object,
    path: str,
    binding_type: str | None,
    changes: list[SnapshotChangeRecord],
) -> None:
    if before is not _MISSING and after is not _MISSING and before == after:
        return

    current_binding_type = binding_type
    for value in (after, before):
        if isinstance(value, Mapping):
            mapped_value = cast(Mapping[str, object], value)
            resource_type = mapped_value.get("resource_type")
            if isinstance(resource_type, str):
                current_binding_type = resource_type
                break

    if isinstance(before, Mapping) and isinstance(after, Mapping):
        before_mapping = cast(Mapping[str, object], before)
        after_mapping = cast(Mapping[str, object], after)
        keys = sorted(set(before_mapping) | set(after_mapping))
        for key in keys:
            _walk_diff(
                before_mapping.get(key, _MISSING),
                after_mapping.get(key, _MISSING),
                _join(path, key),
                current_binding_type,
                changes,
            )
        return
    before_value = cast(object, before)
    after_value = after
    if _is_sequence(before_value) and _is_sequence(after_value):
        before_items = cast(Sequence[object], before_value)
        after_items = cast(Sequence[object], after_value)
        for index in range(max(len(before_items), len(after_items))):
            _walk_diff(
                before_items[index] if index < len(before_items) else _MISSING,
                after_items[index] if index < len(after_items) else _MISSING,
                _join(path, str(index)),
                current_binding_type,
                changes,
            )
        return

    change_type = (
        "added" if before is _MISSING else "removed" if after is _MISSING else "changed"
    )
    sensitive = _is_sensitive(path)
    changes.append(
        SnapshotChangeRecord(
            category=_category(path, current_binding_type),
            path=path or "/",
            change_type=change_type,
            before=(
                None
                if before is _MISSING
                else _safe_value(cast(object, before), sensitive=sensitive)
            ),
            after=(
                None if after is _MISSING else _safe_value(after, sensitive=sensitive)
            ),
            sensitive=sensitive,
        )
    )


def _category(path: str, binding_type: str | None) -> SnapshotDiffCategory:
    segments = {segment.lower() for segment in path.split("/") if segment}
    if segments & _SENSITIVE_SEGMENTS or any("secret" in item for item in segments):
        return "secret_reference"
    if "model_routing" in segments or binding_type == "model":
        return "model"
    if binding_type == "sandbox" or "sandbox" in segments:
        return "sandbox"
    if "permission" in segments or "permissions" in segments:
        return "permission"
    if "network" in segments or "egress" in segments:
        return "network"
    if "runtime_type" in segments or "runtime" in segments:
        return "runtime"
    return "resource_version"


def _is_sensitive(path: str) -> bool:
    segments = {segment.lower() for segment in path.split("/") if segment}
    return bool(
        segments & _SENSITIVE_SEGMENTS
        or segments & _BODY_SEGMENTS
        or any("secret" in item or "credential" in item for item in segments)
    )


def _safe_value(value: object, *, sensitive: bool) -> JsonValue:
    if sensitive:
        if isinstance(value, str) and value.startswith("secret://"):
            return value
        return cast(JsonValue, {"redacted_hash": _canonical_hash(value)})
    if value is None or isinstance(value, bool | int | float):
        return cast(JsonValue, value)
    if isinstance(value, str):
        if len(value) <= 256:
            return value
        return cast(
            JsonValue,
            {"type": "string", "length": len(value), "hash": _canonical_hash(value)},
        )
    if isinstance(value, Mapping):
        mapped_value = cast(Mapping[str, object], value)
        return cast(
            JsonValue,
            {
                "type": "object",
                "keys": sorted(mapped_value),
                "hash": _canonical_hash(mapped_value),
            },
        )
    if _is_sequence(value):
        sequence_value = cast(Sequence[object], value)
        return cast(
            JsonValue,
            {
                "type": "array",
                "length": len(sequence_value),
                "hash": _canonical_hash(sequence_value),
            },
        )
    return cast(
        JsonValue, {"type": type(value).__name__, "hash": _canonical_hash(value)}
    )


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, str | bytes | bytearray
    )


def _join(path: str, segment: str) -> str:
    escaped = segment.replace("~", "~0").replace("/", "~1")
    return f"{path}/{escaped}"

"""Write-time minimization for every ORM-backed AuditLog fact."""

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import cast
from uuid import UUID

REDACTED = "[REDACTED]"
MAX_AUDIT_METADATA_DEPTH = 6
MAX_AUDIT_METADATA_ITEMS = 100
MAX_AUDIT_STRING_LENGTH = 512

_SENSITIVE_KEYS = frozenset(
    {
        "arguments",
        "authorization",
        "body",
        "comment",
        "content",
        "cookie",
        "error",
        "file_content",
        "input",
        "nonce",
        "output",
        "password",
        "payload",
        "prompt",
        "request",
        "response",
        "secret",
        "secret_ref",
        "ticket_nonce",
        "token",
        "url",
    }
)
_SENSITIVE_SUFFIXES = (
    "_arguments",
    "_authorization",
    "_body",
    "_comment",
    "_content",
    "_cookie",
    "_nonce",
    "_password",
    "_payload",
    "_prompt",
    "_request",
    "_response",
    "_secret",
    "_secret_ref",
    "_token",
    "_url",
)


def sanitize_audit_metadata(value: Mapping[str, object]) -> dict[str, object]:
    sanitized = _sanitize_mapping(value, depth=0)
    return sanitized


def audit_change_digest(metadata: Mapping[str, object]) -> str:
    canonical = json.dumps(
        metadata,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def infer_audit_run_id(
    *,
    resource_type: str,
    resource_id: UUID | None,
    metadata: Mapping[str, object],
) -> UUID | None:
    if resource_type == "run" and resource_id is not None:
        return resource_id
    candidate = metadata.get("run_id")
    if not isinstance(candidate, str):
        return None
    try:
        return UUID(candidate)
    except ValueError:
        return None


def _sanitize_mapping(value: Mapping[str, object], *, depth: int) -> dict[str, object]:
    if depth >= MAX_AUDIT_METADATA_DEPTH:
        return {"truncated": True}
    result: dict[str, object] = {}
    for index, (raw_key, item) in enumerate(value.items()):
        if index >= MAX_AUDIT_METADATA_ITEMS:
            result["truncated"] = True
            break
        key = str(raw_key)[:128]
        normalized = key.casefold()
        if normalized in _SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_SUFFIXES):
            result[key] = REDACTED
        else:
            result[key] = _sanitize_value(item, depth=depth + 1)
    return result


def _sanitize_value(value: object, *, depth: int) -> object:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else REDACTED
    if isinstance(value, str):
        if len(value) <= MAX_AUDIT_STRING_LENGTH:
            return value
        return {
            "sha256": hashlib.sha256(value.encode()).hexdigest(),
            "length": len(value),
            "truncated": True,
        }
    if isinstance(value, Mapping):
        return _sanitize_mapping(cast(Mapping[str, object], value), depth=depth)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        sequence = cast(Sequence[object], value)
        if depth >= MAX_AUDIT_METADATA_DEPTH:
            return ["[TRUNCATED]"]
        items = list(sequence[:MAX_AUDIT_METADATA_ITEMS])
        sanitized = [_sanitize_value(item, depth=depth + 1) for item in items]
        if len(sequence) > len(items):
            sanitized.append("[TRUNCATED]")
        return sanitized
    return {"type": type(value).__name__, "redacted": True}

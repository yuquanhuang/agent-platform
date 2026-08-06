"""Canonical request fingerprints shared by idempotent use cases."""

import hashlib
import json
from collections.abc import Iterable
from typing import cast

from pydantic import BaseModel


def canonical_request_hash(
    operation_type: str,
    request: BaseModel | None = None,
    *,
    extra: dict[str, object] | None = None,
    unordered_fields: Iterable[str] = (),
) -> str:
    """Hash a normalized request without logging request contents."""

    payload: dict[str, object] = {"operation_type": operation_type}
    if request is not None:
        body = cast(
            dict[str, object], request.model_dump(mode="json", exclude_unset=True)
        )
        for field in unordered_fields:
            value = body.get(field)
            if isinstance(value, list):
                items = cast(list[object], value)
                if all(isinstance(item, str) for item in items):
                    body[field] = sorted(cast(list[str], items))
        payload["body"] = body
    if extra is not None:
        payload.update(extra)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()

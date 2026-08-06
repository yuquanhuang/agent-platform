"""Composable reference-query port for resource consumers added by later Epics."""

import base64
import json
from collections.abc import Sequence
from typing import Protocol, cast
from uuid import UUID

from packages.contracts.public import TenantContext
from packages.domain.resources import (
    ResourceReferenceRecord,
    ResourceReferenceType,
    ResourceType,
)

REFERENCE_TYPE_ORDER = {
    "draft_binding": 0,
    "snapshot": 1,
    "deployment": 2,
    "schedule": 3,
    "session": 4,
}


class ResourceReferenceProvider(Protocol):
    async def list_references(
        self,
        context: TenantContext,
        *,
        target_type: ResourceType,
        target_id: UUID,
        limit: int,
        after: ResourceReferenceRecord | None,
    ) -> Sequence[ResourceReferenceRecord]: ...


class CompositeResourceReferenceReader:
    """Merge bounded reference providers without inventing a second fact store."""

    def __init__(self, providers: Sequence[ResourceReferenceProvider]) -> None:
        self._providers = tuple(providers)

    async def list_references(
        self,
        context: TenantContext,
        *,
        target_type: ResourceType,
        target_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[ResourceReferenceRecord], str | None]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        after = _decode_cursor(cursor) if cursor is not None else None
        rows: set[ResourceReferenceRecord] = set()
        for provider in self._providers:
            rows.update(
                await provider.list_references(
                    context,
                    target_type=target_type,
                    target_id=target_id,
                    limit=limit + 1,
                    after=after,
                )
            )
        ordered = sorted(rows, key=_reference_key)
        if after is not None:
            ordered = [
                row for row in ordered if _reference_key(row) > _reference_key(after)
            ]
        page = ordered[:limit]
        next_cursor = (
            _encode_cursor(page[-1]) if len(ordered) > limit and page else None
        )
        return page, next_cursor


def _reference_key(reference: ResourceReferenceRecord) -> tuple[str, str, str, str]:
    return (
        str(REFERENCE_TYPE_ORDER[reference.reference_type]),
        reference.resource_type,
        str(reference.resource_id),
        str(reference.version_id or ""),
    )


def _encode_cursor(reference: ResourceReferenceRecord) -> str:
    encoded = json.dumps(
        {
            "reference_type": reference.reference_type,
            "resource_type": reference.resource_type,
            "resource_id": str(reference.resource_id),
            "version_id": str(reference.version_id) if reference.version_id else None,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(encoded).decode().rstrip("=")


def _decode_cursor(value: str) -> ResourceReferenceRecord:
    try:
        padding = "=" * (-len(value) % 4)
        decoded: object = json.loads(base64.urlsafe_b64decode(value + padding))
        if not isinstance(decoded, dict):
            raise TypeError
        payload = cast(dict[str, object], decoded)
        reference_type = payload.get("reference_type")
        resource_type = payload.get("resource_type")
        resource_id = payload.get("resource_id")
        version_id = payload.get("version_id")
        if reference_type not in REFERENCE_TYPE_ORDER:
            raise TypeError
        if not isinstance(resource_type, str) or not isinstance(resource_id, str):
            raise TypeError
        if version_id is not None and not isinstance(version_id, str):
            raise TypeError
        return ResourceReferenceRecord(
            resource_type=resource_type,
            resource_id=UUID(resource_id),
            reference_type=cast(ResourceReferenceType, reference_type),
            version_id=UUID(version_id) if version_id is not None else None,
        )
    except (TypeError, ValueError, json.JSONDecodeError, KeyError) as exc:
        raise ValueError("reference cursor is invalid") from exc

"""Canonical idempotency request hashes."""

from pydantic import BaseModel

from packages.application.resources import canonical_request_hash


class Request(BaseModel):
    name: str
    role_ids: list[str]


def test_hash_normalizes_declared_set_semantics_only() -> None:
    first = Request(name="example", role_ids=["b", "a"])
    second = Request(name="example", role_ids=["a", "b"])

    assert canonical_request_hash(
        "resource.create", first, unordered_fields=("role_ids",)
    ) == canonical_request_hash(
        "resource.create", second, unordered_fields=("role_ids",)
    )
    assert canonical_request_hash("resource.create", first) != canonical_request_hash(
        "resource.create", second
    )

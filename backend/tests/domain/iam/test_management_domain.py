"""IAM ETag and opaque cursor domain tests."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from packages.domain.public import decode_cursor, encode_cursor, format_etag, parse_etag


def test_strong_etag_round_trip_uses_frozen_format() -> None:
    assert format_etag(7) == '"rv:7"'
    assert parse_etag('"rv:7"') == 7


@pytest.mark.parametrize("value", ["rv:1", 'W/"rv:1"', '"rv:0"', '"version:1"'])
def test_strong_etag_rejects_non_contract_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_etag(value)


def test_cursor_round_trip_is_opaque_and_stable() -> None:
    created_at = datetime(2026, 8, 6, 3, 4, 5, tzinfo=UTC)
    resource_id = UUID("11111111-1111-4111-8111-111111111111")

    cursor = encode_cursor(created_at, resource_id)

    assert ":" not in cursor
    assert decode_cursor(cursor) == (created_at, resource_id)


def test_cursor_rejects_untrusted_payload() -> None:
    with pytest.raises(ValueError, match="cursor is invalid"):
        decode_cursor("not-a-cursor")

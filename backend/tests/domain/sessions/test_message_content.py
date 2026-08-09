"""Frozen Message content-part validation tests."""

import pytest

from packages.domain.public import parse_message_content_parts


def test_message_content_parts_accept_all_frozen_variants() -> None:
    parts = parse_message_content_parts(
        [
            {"type": "text", "text": "Hello"},
            {"type": "artifact_reference", "artifact_id": "artifact-1"},
            {"type": "tool_reference", "tool_call_id": "tool-call-1"},
            {"type": "error_notice", "error_code": "MODEL_TIMEOUT", "text": "Retry"},
        ]
    )

    assert [part.type for part in parts] == [
        "text",
        "artifact_reference",
        "tool_reference",
        "error_notice",
    ]
    assert parts[-1].error_code == "MODEL_TIMEOUT"


@pytest.mark.parametrize(
    "value",
    [
        [],
        ["not-an-object"],
        [{"type": "text"}],
        [{"type": "artifact_reference", "artifact_id": "artifact-1", "text": "x"}],
        [{"type": "tool_reference", "tool_call_id": ""}],
        [{"type": "error_notice", "error_code": "E", "artifact_id": "a"}],
        [{"type": "text", "text": "x", "html": "<b>x</b>"}],
    ],
)
def test_message_content_parts_reject_invalid_or_unsafe_shapes(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        parse_message_content_parts(value)

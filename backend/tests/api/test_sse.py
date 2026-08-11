"""Bounded SSE response backpressure tests."""

import asyncio
from collections.abc import AsyncIterator

import pytest
from starlette.types import Message

from apps.api.sse import BoundedSseResponse


@pytest.mark.asyncio
async def test_slow_consumer_timeout_closes_stream_generator() -> None:
    closed = False
    sent: list[Message] = []

    async def frames() -> AsyncIterator[bytes]:
        nonlocal closed
        try:
            yield b"id: 1\nevent: run_event\ndata: {}\n\n"
        finally:
            closed = True

    async def slow_send(message: Message) -> None:
        sent.append(message)
        if message["type"] == "http.response.body":
            await asyncio.Event().wait()

    response = BoundedSseResponse(
        frames(), send_timeout_seconds=0.01, media_type="text/event-stream"
    )

    await response.stream_response(slow_send)

    assert sent[0]["type"] == "http.response.start"
    assert sent[1]["type"] == "http.response.body"
    assert closed is True

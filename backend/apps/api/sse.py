"""Bounded ASGI response behavior for Server-Sent Events."""

import asyncio
from collections.abc import AsyncIterable, Mapping

from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse
from starlette.types import Send

from packages.infrastructure.observability import PlatformMetrics


class BoundedSseResponse(StreamingResponse):
    """Stop producing when a client cannot accept one frame within the bound."""

    def __init__(
        self,
        content: AsyncIterable[bytes],
        *,
        send_timeout_seconds: float,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        media_type: str | None = None,
        background: BackgroundTask | None = None,
        metrics: PlatformMetrics | None = None,
    ) -> None:
        if send_timeout_seconds <= 0:
            raise ValueError("SSE send timeout must be positive")
        self._send_timeout_seconds = send_timeout_seconds
        self._metrics = metrics
        super().__init__(
            content,
            status_code=status_code,
            headers=headers,
            media_type=media_type,
            background=background,
        )

    async def stream_response(self, send: Send) -> None:
        try:
            await asyncio.wait_for(
                send(
                    {
                        "type": "http.response.start",
                        "status": self.status_code,
                        "headers": self.raw_headers,
                    }
                ),
                timeout=self._send_timeout_seconds,
            )
            async for chunk in self.body_iterator:
                if not isinstance(chunk, bytes | memoryview):
                    chunk = chunk.encode(self.charset)
                await asyncio.wait_for(
                    send(
                        {
                            "type": "http.response.body",
                            "body": chunk,
                            "more_body": True,
                        }
                    ),
                    timeout=self._send_timeout_seconds,
                )
            await asyncio.wait_for(
                send(
                    {
                        "type": "http.response.body",
                        "body": b"",
                        "more_body": False,
                    }
                ),
                timeout=self._send_timeout_seconds,
            )
        except TimeoutError:
            if self._metrics is not None:
                self._metrics.observe_sse_connection_outcome(outcome="send_timeout")
            await self._close_body_iterator()

    async def _close_body_iterator(self) -> None:
        close = getattr(self.body_iterator, "aclose", None)
        if close is not None:
            await close()

"""Strict Outbox event routing without broadening individual handlers."""

from collections.abc import Mapping

from packages.application.outbox.dispatcher import (
    PermanentOutboxError,
    WorkflowStarter,
    WorkflowStartResult,
)
from packages.domain.outbox import OutboxEvent


class OutboxEventRouter:
    def __init__(self, routes: Mapping[str, WorkflowStarter]) -> None:
        self._routes = dict(routes)

    async def start(self, event: OutboxEvent) -> WorkflowStartResult:
        handler = self._routes.get(event.event_type)
        if handler is None:
            raise PermanentOutboxError(f"unsupported event_type: {event.event_type}")
        return await handler.start(event)

"""Strict Outbox event routing without broadening individual handlers."""

from collections.abc import Mapping
from datetime import datetime

from packages.application.outbox.dispatcher import (
    PermanentOutboxError,
    WorkflowStarter,
    WorkflowStartResult,
    WorkflowStartResultRecorder,
)
from packages.contracts.public import TenantContext
from packages.domain.outbox import OutboxEvent


class OutboxEventRouter:
    def __init__(self, routes: Mapping[str, WorkflowStarter]) -> None:
        self._routes = dict(routes)

    async def start(self, event: OutboxEvent) -> WorkflowStartResult:
        handler = self._routes.get(event.event_type)
        if handler is None:
            raise PermanentOutboxError(f"unsupported event_type: {event.event_type}")
        return await handler.start(event)


class OutboxStartResultRouter:
    """Route optional post-start persistence without coupling the dispatcher to events."""

    def __init__(self, routes: Mapping[str, WorkflowStartResultRecorder]) -> None:
        self._routes = dict(routes)

    async def record(
        self,
        context: TenantContext,
        event: OutboxEvent,
        result: WorkflowStartResult,
        *,
        now: datetime,
    ) -> None:
        recorder = self._routes.get(event.event_type)
        if recorder is not None:
            await recorder.record(context, event, result, now=now)

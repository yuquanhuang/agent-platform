"""Formal Temporal Outbox Router composition tests."""

from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client

from apps.event_worker import composition
from apps.event_worker.composition import build_temporal_outbox_dispatcher
from packages.application.outbox import OutboxDispatcher, OutboxStore, WorkflowStarter
from packages.infrastructure.config import AppSettings
from packages.infrastructure.observability import PlatformMetrics


def test_composition_registers_frozen_temporal_routes() -> None:
    dispatcher = build_temporal_outbox_dispatcher(
        AppSettings(outbox_batch_size=7, outbox_max_attempts=4),
        session_factory=async_sessionmaker(class_=AsyncSession),
        temporal_client=cast(Client, object()),
        metrics=PlatformMetrics(),
    )

    assert isinstance(dispatcher, OutboxDispatcher)


def test_composition_rejects_run_route_override() -> None:
    with pytest.raises(ValueError, match="cannot be overridden"):
        build_temporal_outbox_dispatcher(
            AppSettings(),
            session_factory=async_sessionmaker(class_=AsyncSession),
            temporal_client=cast(Client, object()),
            metrics=PlatformMetrics(),
            additional_routes={
                "agent.run_requested.v1": cast(WorkflowStarter, object())
            },
        )


def test_composition_limits_claims_to_registered_temporal_event_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, frozenset[str] | None] = {}

    def capture_store(
        _session_factory: async_sessionmaker[AsyncSession],
        *,
        event_types: frozenset[str] | None = None,
    ) -> OutboxStore:
        captured["event_types"] = event_types
        return cast(OutboxStore, object())

    monkeypatch.setattr(composition, "SqlAlchemyOutboxStore", capture_store)
    build_temporal_outbox_dispatcher(
        AppSettings(),
        session_factory=async_sessionmaker(class_=AsyncSession),
        temporal_client=cast(Client, object()),
        metrics=PlatformMetrics(),
        additional_routes={"custom.workflow.v1": cast(WorkflowStarter, object())},
    )

    event_types = captured["event_types"]
    assert event_types == frozenset(
        {
            "agent.run_requested.v1",
            "agent.release_requested.v1",
            "custom.workflow.v1",
            "platform_probe_requested.v1",
        }
    )
    assert event_types is not None
    assert "run.events_appended.v1" not in event_types

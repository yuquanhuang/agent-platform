"""Process boundary entrypoint tests."""

import importlib

import pytest

RESERVED_PROCESS_MODULES = (
    "apps.runtime_worker_agentscope.main",
    "apps.runtime_worker_codex.main",
    "apps.event_worker.main",
    "apps.sandbox_manager.main",
    "apps.reconciliation_worker.main",
)


@pytest.mark.parametrize("module_name", RESERVED_PROCESS_MODULES)
def test_reserved_process_entrypoint_fails_closed(module_name: str) -> None:
    module = importlib.import_module(module_name)

    with pytest.raises(SystemExit, match="reserved but not implemented"):
        module.main()


@pytest.mark.parametrize(
    ("module_name", "expected_kind"),
    (
        ("apps.temporal_worker_control.main", "control"),
        ("apps.temporal_worker_run.main", "run"),
    ),
)
def test_temporal_worker_entrypoint_has_dedicated_kind(
    module_name: str, expected_kind: str
) -> None:
    module = importlib.import_module(module_name)

    assert module.TemporalWorkerKind(expected_kind).value == expected_kind

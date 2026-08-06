"""Temporal probe payload contract tests."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from packages.application.temporal import probe_workflow_id
from packages.contracts.temporal import TemporalWorkerKind, WorkflowProbeInput


def test_probe_contract_forbids_undeclared_history_payload() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        WorkflowProbeInput.model_validate(
            {
                "tenant_id": "11111111-1111-4111-8111-111111111111",
                "probe_id": "22222222-2222-4222-8222-222222222222",
                "worker_kind": "run",
                "request_id": "req-probe",
                "trace_id": "trace-probe",
                "prompt": "must not enter workflow history",
            }
        )


def test_probe_workflow_id_is_deterministic_and_tenant_scoped() -> None:
    tenant_id = UUID("11111111-1111-4111-8111-111111111111")
    probe_id = UUID("22222222-2222-4222-8222-222222222222")

    assert probe_workflow_id(tenant_id, probe_id) == (
        "probe/11111111-1111-4111-8111-111111111111/"
        "22222222-2222-4222-8222-222222222222"
    )
    assert TemporalWorkerKind.RUN.value == "run"

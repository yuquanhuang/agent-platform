import pytest

from packages.capacity_testing.config import CapacityPlan
from packages.capacity_testing.runner import execute_plan


@pytest.mark.asyncio
async def test_dry_run_never_resolves_secret_or_calls_network() -> None:
    plan = CapacityPlan.model_validate(
        {
            "plan_id": "dry-run",
            "scenario": "sse",
            "dry_run": True,
            "request": {
                "base_url": "https://example.invalid",
                "auth_token_env": "MISSING_CAPACITY_SECRET",
            },
            "sse": {"run_ids": ["run-1"], "connections": 1},
        }
    )

    report = await execute_plan(plan)

    assert report["status"] == "dry_run"
    assert report["metrics"] == {"mode": "dry_run"}
    assert "MISSING_CAPACITY_SECRET" not in str(report)

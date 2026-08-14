from pathlib import Path

import pytest

from packages.capacity_testing.config import load_plan


def test_load_plan_defaults_and_does_not_require_secrets(tmp_path: Path) -> None:
    path = tmp_path / "plan.yaml"
    path.write_text(
        """
plan_id: smoke
scenario: agentscope_runs
dry_run: true
request:
  base_url: https://example.invalid
  auth_token_env: CAPACITY_TOKEN
run:
  session_ids: [session-1]
  total_runs: 1
  concurrency: 1
""",
        encoding="utf-8",
    )

    plan = load_plan(path)

    assert plan.dry_run is True
    assert plan.run is not None
    assert plan.run.target is not None
    assert plan.run.target.target == 100
    assert plan.run.target.headroom_ratio == 0.30


def test_run_concurrency_requires_enough_sessions(tmp_path: Path) -> None:
    path = tmp_path / "plan.yaml"
    path.write_text(
        """
plan_id: invalid
scenario: codex_runs
request:
  base_url: https://example.invalid
run:
  session_ids: [session-1]
  concurrency: 2
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="session_ids"):
        load_plan(path)


def test_unknown_fields_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "plan.yaml"
    path.write_text(
        """
plan_id: invalid
scenario: sse
request:
  base_url: https://example.invalid
sse:
  run_ids: [run-1]
  connections: 1
unexpected: true
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unexpected"):
        load_plan(path)


def test_sensitive_static_headers_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "plan.yaml"
    path.write_text(
        """
plan_id: invalid-secret
scenario: sse
request:
  base_url: https://example.invalid
  static_headers:
    Authorization: Bearer leaked
sse:
  run_ids: [run-1]
  connections: 1
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="secret_headers_env"):
        load_plan(path)

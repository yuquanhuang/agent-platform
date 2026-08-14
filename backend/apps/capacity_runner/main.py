"""Run one AP-E7-005 capacity plan and write JSON/Markdown reports."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from packages.capacity_testing.config import load_plan
from packages.capacity_testing.report import write_report
from packages.capacity_testing.runner import execute_plan


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an AP-E7-005 capacity plan")
    parser.add_argument("plan", type=Path, help="YAML/JSON workload plan")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/ap-e7-005/capacity-report"),
        help="Output path without extension",
    )
    args = parser.parse_args()
    plan = load_plan(args.plan)
    report = asyncio.run(execute_plan(plan))
    json_path, markdown_path = write_report(report, args.output)
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {markdown_path}")
    if report["status"] in {"failed", "blocked"}:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

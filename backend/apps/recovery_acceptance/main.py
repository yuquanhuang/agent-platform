"""Evaluate AP-E7-007 recovery and production-readiness evidence."""

from __future__ import annotations

import argparse
from pathlib import Path

from packages.recovery_acceptance import evaluate_plan, load_plan, write_report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run AP-E7-007 recovery acceptance")
    parser.add_argument("plan", type=Path, help="Recovery acceptance YAML plan")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/ap-e7-007/recovery-report"),
        help="Output path without extension",
    )
    args = parser.parse_args()
    plan, base_directory = load_plan(args.plan)
    report = evaluate_plan(plan, base_directory=base_directory)
    json_path, markdown_path = write_report(report, args.output)
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {markdown_path}")
    if report["status"] == "failed":
        raise SystemExit(2)


if __name__ == "__main__":
    main()

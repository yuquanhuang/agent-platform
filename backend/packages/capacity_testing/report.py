"""Machine-readable and human-readable AP-E7-005 result reports."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class CapacityReport(dict[str, Any]):
    """Dictionary-backed report to keep JSON output stable and extensible."""

    @classmethod
    def new(
        cls, *, plan_id: str, scenario: str, metadata: dict[str, str]
    ) -> CapacityReport:
        now = datetime.now(UTC).isoformat()
        return cls(
            report_version="1",
            plan_id=plan_id,
            scenario=scenario,
            status="running",
            started_at=now,
            ended_at=None,
            metadata=dict(metadata),
            metrics={},
            acceptance=[],
            blockers=[],
            follow_up=[],
        )

    def finish(self, *, status: str) -> None:
        self["status"] = status
        self["ended_at"] = datetime.now(UTC).isoformat()


def write_report(report: CapacityReport, output: str | Path) -> tuple[Path, Path]:
    """Write JSON and Markdown without including secret header values."""

    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    json_path = output_path.with_suffix(".json")
    markdown_path = output_path.with_suffix(".md")
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def _markdown(report: CapacityReport) -> str:
    lines = [
        f"# AP-E7-005 Capacity Report: `{report['plan_id']}`",
        "",
        f"- Scenario: `{report['scenario']}`",
        f"- Status: **{report['status']}**",
        f"- Started: `{report['started_at']}`",
        f"- Ended: `{report['ended_at'] or 'running'}`",
        "",
        "## Metrics",
        "",
        "```json",
        json.dumps(report.get("metrics", {}), indent=2, sort_keys=True),
        "```",
        "",
        "## Acceptance",
        "",
        "| Check | Actual | Required | Headroom | Result |",
        "|---|---:|---:|---:|---|",
    ]
    for item in report.get("acceptance", []):
        lines.append(
            f"| {item['name']} | {item.get('actual', 'n/a')} {item.get('unit', '')} | "
            f"{item['required']} {item.get('unit', '')} | {item['headroom_ratio']:.0%} | "
            f"{item['status']} |"
        )
    lines.extend(["", "## Blockers", ""])
    lines.extend(f"- {item}" for item in report.get("blockers", []))
    lines.extend(["", "## Follow-up", ""])
    lines.extend(f"- {item}" for item in report.get("follow_up", []))
    return "\n".join(lines) + "\n"

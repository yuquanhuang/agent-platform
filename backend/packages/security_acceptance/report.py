"""JSON and Markdown reports for AP-E7-006."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class SecurityAcceptanceReport(dict[str, Any]):
    @classmethod
    def new(
        cls, *, plan_id: str, mode: str, metadata: dict[str, str]
    ) -> SecurityAcceptanceReport:
        return cls(
            report_version="1",
            plan_id=plan_id,
            mode=mode,
            status="running",
            started_at=datetime.now(UTC).isoformat(),
            ended_at=None,
            metadata=dict(metadata),
            checks=[],
            blockers=[],
            follow_up=[],
        )

    def add_check(
        self,
        *,
        name: str,
        passed: bool,
        message: str,
        blocking: bool = True,
    ) -> None:
        self["checks"].append(
            {
                "name": name,
                "status": "PASS" if passed else "FAIL",
                "blocking": blocking,
                "message": message,
            }
        )
        if blocking and not passed:
            self["blockers"].append(message)

    def finish(self) -> None:
        if self["mode"] == "dry_run":
            self["status"] = "dry_run"
        elif self["blockers"]:
            self["status"] = "failed"
        else:
            self["status"] = "passed"
        self["ended_at"] = datetime.now(UTC).isoformat()


def write_report(
    report: SecurityAcceptanceReport, output: str | Path
) -> tuple[Path, Path]:
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    json_path = output_path.with_suffix(".json")
    markdown_path = output_path.with_suffix(".md")
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def _markdown(report: SecurityAcceptanceReport) -> str:
    lines = [
        f"# AP-E7-006 Security Report: `{report['plan_id']}`",
        "",
        f"- Mode: `{report['mode']}`",
        f"- Status: **{report['status']}**",
        f"- Started: `{report['started_at']}`",
        f"- Ended: `{report['ended_at'] or 'running'}`",
        "",
        "## Checks",
        "",
        "| Check | Result | Blocking | Message |",
        "|---|---|---|---|",
    ]
    for check in report["checks"]:
        message = str(check["message"]).replace("|", "\\|")
        lines.append(
            f"| {check['name']} | {check['status']} | {check['blocking']} | {message} |"
        )
    lines.extend(["", "## Blockers", ""])
    lines.extend(f"- {item}" for item in report["blockers"])
    lines.extend(["", "## Follow-up", ""])
    lines.extend(f"- {item}" for item in report["follow_up"])
    return "\n".join(lines) + "\n"

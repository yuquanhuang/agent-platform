#!/usr/bin/env python3
"""Run configured quality gates for affected areas of a full-stack monorepo."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "harness" / "project.json"
ORDER = ("contracts", "backend", "frontend")


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"Missing Harness configuration: {CONFIG_PATH}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {CONFIG_PATH}: {exc}") from exc


def git_output(arguments: list[str]) -> list[str]:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def changed_paths() -> set[str]:
    names = set(git_output(["diff", "--name-only", "--diff-filter=ACMR", "HEAD"]))
    names.update(git_output(["ls-files", "--others", "--exclude-standard"]))
    return names


def is_within(path: str, root: str) -> bool:
    normalized = root.strip("/")
    return path == normalized or path.startswith(f"{normalized}/")


def affected_scopes(config: dict) -> list[str]:
    changed = changed_paths()
    if not changed:
        return []

    scopes = config.get("scopes", {})
    affected: set[str] = set()
    shared_paths = [str(item) for item in config.get("shared_paths", [])]

    if any(is_within(path, shared) for path in changed for shared in shared_paths):
        affected.update(name for name in ("backend", "frontend") if name in scopes)

    for name, scope in scopes.items():
        root = str(scope.get("path", name))
        if any(is_within(path, root) for path in changed):
            affected.add(name)
            affected.update(str(item) for item in scope.get("affects", []))

    return [name for name in ORDER if name in affected]


def resolve_scopes(config: dict, requested: str) -> list[str]:
    scopes = config.get("scopes", {})
    if requested == "auto":
        return affected_scopes(config)
    if requested == "all":
        return [name for name in ORDER if name in scopes]
    if requested not in scopes:
        raise SystemExit(f"Unknown or unconfigured scope: {requested}")
    selected = {requested, *map(str, scopes[requested].get("affects", []))}
    return [name for name in ORDER if name in selected]


def expand_command(command: list[str]) -> list[str]:
    return [part.replace("{python}", sys.executable) for part in command]


def run_scope_step(
    config: dict,
    scope_name: str,
    step: str,
    *,
    dry_run: bool,
) -> int:
    scope = config["scopes"][scope_name]
    raw_command = scope.get("commands", {}).get(step, [])
    if not raw_command:
        print(f"Skip {scope_name}:{step}: no command configured.")
        return 0

    command = expand_command([str(item) for item in raw_command])
    cwd = PROJECT_ROOT / str(scope.get("path", scope_name))
    print(f"[{scope_name}] +", " ".join(command), f"(cwd={cwd})")
    if dry_run:
        return 0
    if not cwd.is_dir():
        print(f"Configured scope directory does not exist: {cwd}", file=sys.stderr)
        return 2
    try:
        return subprocess.run(command, cwd=cwd, check=False).returncode
    except FileNotFoundError:
        print(f"Command not found for {scope_name}: {command[0]}", file=sys.stderr)
        return 127


def run_for_scopes(
    config: dict,
    scopes: list[str],
    command: str,
    *,
    dry_run: bool,
) -> int:
    if not scopes:
        print("No affected scopes. Use --scope all to run the complete gate.")
        return 0

    for scope in scopes:
        if command == "check":
            steps = [str(item) for item in config["scopes"][scope].get("check_steps", [])]
        elif command == "contract_check":
            steps = ["contract_check"] if scope == "contracts" else []
        else:
            steps = [command]

        for step in steps:
            result = run_scope_step(config, scope, step, dry_run=dry_run)
            if result != 0:
                return result
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "format",
            "format-check",
            "lint",
            "typecheck",
            "test",
            "build",
            "contract-check",
            "check",
        ),
    )
    parser.add_argument(
        "--scope",
        default="auto",
        choices=("auto", "backend", "frontend", "contracts", "all"),
    )
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running them.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config()
    scopes = resolve_scopes(config, args.scope)
    command = args.command.replace("-", "_")
    return run_for_scopes(config, scopes, command, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

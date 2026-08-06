"""Run the fail-closed Epic 0 full-stack acceptance profile."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import IO
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
INTEGRATION_TESTS = (
    "tests/integration/database/test_postgresql_rls.py",
    "tests/integration/iam/test_current_identity.py",
    "tests/integration/iam/test_iam_management.py",
    "tests/integration/temporal/test_probe_workflow.py",
)


class AcceptanceError(RuntimeError):
    """Epic 0 acceptance cannot produce trustworthy PASS evidence."""


def validate_environment(environment: Mapping[str, str]) -> None:
    database_url = environment.get("AP_TEST_DATABASE_URL")
    if not database_url:
        raise AcceptanceError("AP_TEST_DATABASE_URL is required")
    parsed = urlparse(database_url)
    database_name = parsed.path.rsplit("/", maxsplit=1)[-1]
    if parsed.scheme != "postgresql+asyncpg" or not database_name.endswith("_test"):
        raise AcceptanceError(
            "AP_TEST_DATABASE_URL must use postgresql+asyncpg and a *_test database"
        )
    if environment.get("AP_TEST_TEMPORAL") != "1":
        raise AcceptanceError("AP_TEST_TEMPORAL=1 is required")


def run_command(
    command: Sequence[str], *, cwd: Path, environment: Mapping[str, str]
) -> None:
    print("+", " ".join(command), f"(cwd={cwd})", flush=True)
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=dict(environment),
        check=False,
    )
    if completed.returncode != 0:
        raise AcceptanceError(
            f"command failed with exit code {completed.returncode}: {command[0]}"
        )


def assert_zero_skip_junit(report_path: Path) -> int:
    root = ET.parse(report_path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    tests = sum(int(suite.attrib.get("tests", "0")) for suite in suites)
    failures = sum(int(suite.attrib.get("failures", "0")) for suite in suites)
    errors = sum(int(suite.attrib.get("errors", "0")) for suite in suites)
    skipped = sum(int(suite.attrib.get("skipped", "0")) for suite in suites)
    if tests < len(INTEGRATION_TESTS):
        raise AcceptanceError(
            f"expected at least {len(INTEGRATION_TESTS)} integration tests, found {tests}"
        )
    if failures or errors or skipped:
        raise AcceptanceError(
            "integration evidence is not clean: "
            f"failures={failures}, errors={errors}, skipped={skipped}"
        )
    return tests


def available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_for_http(
    url: str,
    *,
    process: subprocess.Popen[bytes],
    timeout_seconds: float = 20.0,
) -> bytes:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise AcceptanceError(
                f"process exited before {url} became ready: exit={return_code}"
            )
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status != 200:
                    raise AcceptanceError(f"unexpected HTTP {response.status}: {url}")
                return response.read()
        except (OSError, urllib.error.URLError) as error:
            last_error = error
            time.sleep(0.1)
    raise AcceptanceError(f"timed out waiting for {url}: {type(last_error).__name__}")


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def start_process(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    log: IO[bytes],
) -> subprocess.Popen[bytes]:
    print("+", " ".join(command), f"(cwd={cwd})", flush=True)
    return subprocess.Popen(
        command,
        cwd=cwd,
        env=dict(environment),
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def run_cross_stack_smoke(environment: Mapping[str, str]) -> None:
    api_port = available_loopback_port()
    web_port = available_loopback_port()
    while web_port == api_port:
        web_port = available_loopback_port()
    with tempfile.TemporaryDirectory(prefix="agent-platform-epic0-smoke-") as temp_dir:
        temp_root = Path(temp_dir)
        with ExitStack() as stack:
            api_log = stack.enter_context((temp_root / "api.log").open("wb"))
            web_log = stack.enter_context((temp_root / "web.log").open("wb"))
            api_environment = dict(environment)
            api_environment.update(
                {
                    "AP_ENV": "test",
                    "AP_SERVICE_NAME": "api-epic0-acceptance",
                    "AP_AUTH_MODE": "mock",
                    "AP_API_HOST": "127.0.0.1",
                    "AP_API_PORT": str(api_port),
                }
            )
            web_environment = dict(environment)
            web_environment["BACKEND_PROXY_TARGET"] = f"http://127.0.0.1:{api_port}"
            api_process = start_process(
                (
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "apps.api.app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(api_port),
                ),
                cwd=BACKEND_ROOT,
                environment=api_environment,
                log=api_log,
            )
            stack.callback(stop_process, api_process)
            direct_payload = json.loads(
                wait_for_http(
                    f"http://127.0.0.1:{api_port}/health/ready",
                    process=api_process,
                )
            )
            if direct_payload != {
                "status": "ok",
                "service_name": "api-epic0-acceptance",
            }:
                raise AcceptanceError("FastAPI readiness payload is unexpected")
            web_process = start_process(
                (
                    "pnpm",
                    "dev",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(web_port),
                    "--strictPort",
                ),
                cwd=FRONTEND_ROOT,
                environment=web_environment,
                log=web_log,
            )
            stack.callback(stop_process, web_process)
            index_html = wait_for_http(
                f"http://127.0.0.1:{web_port}/", process=web_process
            ).decode("utf-8")
            if '<div id="app"></div>' not in index_html:
                raise AcceptanceError("Vite did not serve the Vue application shell")
            proxied_payload = json.loads(
                wait_for_http(
                    f"http://127.0.0.1:{web_port}/health/ready",
                    process=web_process,
                )
            )
            if proxied_payload != direct_payload:
                raise AcceptanceError(
                    "Vite readiness proxy changed the backend payload"
                )


def run_acceptance(environment: Mapping[str, str]) -> dict[str, object]:
    validate_environment(environment)
    run_command(("make", "check-all"), cwd=PROJECT_ROOT, environment=environment)
    with tempfile.TemporaryDirectory(prefix="agent-platform-epic0-junit-") as temp_dir:
        report_path = Path(temp_dir) / "integration.xml"
        run_command(
            (
                sys.executable,
                "-m",
                "pytest",
                *INTEGRATION_TESTS,
                "-q",
                f"--junitxml={report_path}",
            ),
            cwd=BACKEND_ROOT,
            environment=environment,
        )
        integration_tests = assert_zero_skip_junit(report_path)
    run_cross_stack_smoke(environment)
    return {
        "status": "PASS",
        "baseline": "agent-platform-v1-dev-baseline-2026-08-r5",
        "integration_tests": integration_tests,
        "cross_stack_smoke": "PASS",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the required profile without connecting to dependencies.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.dry_run:
        print("Required environment: AP_TEST_DATABASE_URL, AP_TEST_TEMPORAL=1")
        print("Steps: make check-all -> zero-skip integrations -> API/Vite proxy smoke")
        return 0
    try:
        result = run_acceptance(os.environ)
    except AcceptanceError as error:
        print(f"Epic 0 acceptance FAILED: {error}", file=sys.stderr)
        return 1
    print("Epic 0 acceptance PASSED:", json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Async HTTP workloads for AP-E7-005.

The runner deliberately does not fabricate runtime events or replace dependencies. A
dry run validates configuration only; a real run requires explicit URLs, credentials,
session/run facts and workload-specific service composition.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx

from packages.capacity_testing.config import AcceptanceTarget, CapacityPlan
from packages.capacity_testing.report import CapacityReport
from packages.capacity_testing.stats import SampleStats

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED", "TIMEOUT"}


async def execute_plan(plan: CapacityPlan) -> CapacityReport:
    report = CapacityReport.new(
        plan_id=plan.plan_id, scenario=plan.scenario, metadata=plan.metadata
    )
    if plan.dry_run:
        report["metrics"] = {"mode": "dry_run"}
        report["follow_up"] = [
            "Run the same plan with dry_run=false in an isolated environment.",
            "Attach cluster, dependency versions and Prometheus resource snapshots.",
        ]
        report.finish(status="dry_run")
        return report

    try:
        headers = plan.request.headers()
    except RuntimeError as error:
        report["blockers"] = [str(error)]
        report.finish(status="blocked")
        return report

    timeout = httpx.Timeout(plan.request.request_timeout_seconds)
    limits = httpx.Limits(max_connections=max(100, _connection_limit(plan)))
    async with httpx.AsyncClient(
        base_url=plan.request.base_url.rstrip("/"),
        headers=headers,
        timeout=timeout,
        verify=plan.request.verify_tls,
        limits=limits,
    ) as client:
        stop_sampling = asyncio.Event()
        sampling_task = asyncio.create_task(
            _sample_prometheus(plan, report, stop_sampling)
        )
        try:
            if plan.scenario in {"agentscope_runs", "codex_runs"}:
                await _run_creation_workload(client, plan, report)
            elif plan.scenario == "event_store":
                await _event_store_workload(client, plan, report)
            else:
                await _sse_workload(client, plan, report)
        finally:
            stop_sampling.set()
            await sampling_task
    _evaluate_acceptance(report, _target(plan))
    if report["blockers"]:
        report.finish(status="blocked")
    elif any(item["status"] == "FAIL" for item in report["acceptance"]):
        report.finish(status="failed")
    else:
        report.finish(status="passed")
    return report


def _connection_limit(plan: CapacityPlan) -> int:
    if plan.sse is not None:
        return plan.sse.connections + 50
    if plan.run is not None:
        return plan.run.concurrency + 50
    return max(100, len(plan.event_store.targets) * 2) if plan.event_store else 100


def _target(plan: CapacityPlan) -> AcceptanceTarget:
    if plan.run is not None:
        assert plan.run.target is not None
        return plan.run.target
    if plan.event_store is not None:
        assert plan.event_store.target is not None
        return plan.event_store.target
    assert plan.sse is not None and plan.sse.target is not None
    return plan.sse.target


async def _run_creation_workload(
    client: httpx.AsyncClient, plan: CapacityPlan, report: CapacityReport
) -> None:
    run = plan.run
    assert run is not None
    create_stats = SampleStats()
    total_stats = SampleStats()
    successes = failures = 0
    peak_in_flight = 0
    in_flight = 0
    lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(run.concurrency)

    async def one(index: int) -> None:
        nonlocal successes, failures, in_flight, peak_in_flight
        async with semaphore:
            async with lock:
                in_flight += 1
                peak_in_flight = max(peak_in_flight, in_flight)
            started = time.perf_counter()
            body: dict[str, Any] = {
                "session_id": run.session_ids[index % len(run.session_ids)],
                "client_request_id": f"capacity-{plan.plan_id}-{index}",
                "input": {"text": run.input_text},
                "execution": {
                    "deployment_id": run.deployment_id,
                    "timeout_seconds": run.timeout_seconds,
                },
            }
            try:
                response = await client.post(
                    "/api/v1/runs",
                    json=body,
                    headers={"Idempotency-Key": f"capacity-{uuid4()}"},
                )
                create_stats.observe(
                    time.perf_counter() - started, error=response.status_code != 202
                )
                if response.status_code != 202:
                    failures += 1
                    return
                payload = response.json()
                run_id = payload.get("run_id")
                if not isinstance(run_id, str):
                    failures += 1
                    report["blockers"].append(
                        "Run create response did not contain run_id"
                    )
                    return
                terminal = await _wait_for_terminal(client, run_id, run)
                total_stats.observe(
                    time.perf_counter() - started, error=terminal != "SUCCEEDED"
                )
                if terminal == "SUCCEEDED":
                    successes += 1
                else:
                    failures += 1
            except (httpx.HTTPError, ValueError, KeyError) as error:
                failures += 1
                report["blockers"].append(
                    f"Run workload request failed: {type(error).__name__}"
                )
            finally:
                async with lock:
                    in_flight -= 1

    await asyncio.gather(*(one(index) for index in range(run.total_runs)))
    report["metrics"] = {
        "create_latency": create_stats.as_dict(),
        "run_duration": total_stats.as_dict(),
        "requested_runs": run.total_runs,
        "successful_runs": successes,
        "failed_runs": failures,
        "peak_in_flight": peak_in_flight,
    }


async def _wait_for_terminal(
    client: httpx.AsyncClient, run_id: str, scenario: Any
) -> str:
    deadline = time.monotonic() + scenario.timeout_seconds
    while time.monotonic() < deadline:
        response = await client.get(f"/api/v1/runs/{run_id}")
        if response.status_code != 200:
            await asyncio.sleep(scenario.poll_interval_seconds)
            continue
        status = response.json().get("status")
        if status in TERMINAL_STATUSES:
            return str(status)
        await asyncio.sleep(scenario.poll_interval_seconds)
    return "TIMEOUT"


async def _event_store_workload(
    client: httpx.AsyncClient, plan: CapacityPlan, report: CapacityReport
) -> None:
    event_store = plan.event_store
    assert event_store is not None
    stats = SampleStats()
    sent_events = created_events = failed_batches = 0
    interval = (
        event_store.batch_size
        * len(event_store.targets)
        / event_store.events_per_second
    )
    deadline = time.monotonic() + event_store.duration_seconds

    async def target_loop(target: Any) -> None:
        nonlocal sent_events, created_events, failed_batches
        token = os.environ.get(target.fencing_token_env)
        if not token:
            report["blockers"].append(
                f"Environment variable {target.fencing_token_env!r} is required"
            )
            return
        while time.monotonic() < deadline:
            events = [
                {
                    "source_event_id": str(uuid4()),
                    "event_type": "text_delta",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "payload_version": "1.0",
                    "payload": {"message_id": "capacity", "delta": "x"},
                }
                for _ in range(event_store.batch_size)
            ]
            started = time.perf_counter()
            try:
                response = await client.post(
                    f"/internal/v1/runs/{target.run_id}/events:batch",
                    json={
                        "execution_attempt": target.execution_attempt,
                        "execution_fencing_token": token,
                        "events": events,
                    },
                )
                stats.observe(
                    time.perf_counter() - started, error=response.status_code != 200
                )
                sent_events += len(events)
                if response.status_code == 200:
                    items = response.json().get("items", [])
                    created_events += sum(
                        item.get("status") == "created" for item in items
                    )
                else:
                    failed_batches += 1
            except httpx.HTTPError:
                failed_batches += 1
            await asyncio.sleep(max(0, interval - (time.perf_counter() - started)))

    await asyncio.gather(*(target_loop(target) for target in event_store.targets))
    elapsed = event_store.duration_seconds
    report["metrics"] = {
        "batch_latency": stats.as_dict(),
        "duration_seconds": elapsed,
        "sent_events": sent_events,
        "created_events": created_events,
        "failed_batches": failed_batches,
        "observed_events_per_second": created_events / elapsed if elapsed else 0,
        "configured_events_per_second": event_store.events_per_second,
    }


async def _sse_workload(
    client: httpx.AsyncClient, plan: CapacityPlan, report: CapacityReport
) -> None:
    sse = plan.sse
    assert sse is not None
    opened_attempts = active = peak_active = failed = frames = reconnects = 0
    frame_stats = SampleStats()
    deadline = time.monotonic() + sse.duration_seconds
    lock = asyncio.Lock()

    async def one(index: int) -> None:
        nonlocal opened_attempts, active, peak_active, failed, frames, reconnects
        run_id = sse.run_ids[index % len(sse.run_ids)]
        after = 0
        reconnect_period = (
            60 / sse.reconnect_ratio_per_minute
            if sse.reconnect_ratio_per_minute
            else float("inf")
        )
        forced_reconnect_at = time.monotonic() + reconnect_period * (
            (index + 1) / sse.connections
        )
        while time.monotonic() < deadline:
            try:
                stop_at = min(deadline, forced_reconnect_at)
                async with asyncio.timeout(max(0.001, stop_at - time.monotonic())):
                    async with client.stream(
                        "GET",
                        f"/api/v1/runs/{run_id}/stream",
                        params={"after": after},
                        timeout=httpx.Timeout(
                            connect=client.timeout.connect,
                            read=None,
                            write=client.timeout.write,
                            pool=client.timeout.pool,
                        ),
                    ) as response:
                        if response.status_code != 200:
                            failed += 1
                            return
                        async with lock:
                            opened_attempts += 1
                            active += 1
                            peak_active = max(peak_active, active)
                        try:
                            async for line in response.aiter_lines():
                                if line.startswith("id:"):
                                    frames += 1
                                    try:
                                        after = int(line.removeprefix("id:").strip())
                                    except ValueError:
                                        failed += 1
                                        return
                                    if sse.slow_consumer_delay_ms:
                                        await asyncio.sleep(
                                            sse.slow_consumer_delay_ms / 1000
                                        )
                        finally:
                            async with lock:
                                active -= 1
                reconnects += 1
                await asyncio.sleep(0.05)
            except (httpx.HTTPError, TimeoutError):
                now = time.monotonic()
                if now >= deadline:
                    return
                if now >= forced_reconnect_at:
                    reconnects += 1
                    forced_reconnect_at += reconnect_period
                    continue
                failed += 1
                return

    await asyncio.gather(*(one(index) for index in range(sse.connections)))
    elapsed = sse.duration_seconds
    frame_stats.observe(frames / elapsed if elapsed else 0)
    report["metrics"] = {
        "requested_connections": sse.connections,
        "opened_attempts": opened_attempts,
        "peak_open_connections": peak_active,
        "failed_connections": failed,
        "reconnects": reconnects,
        "frames": frames,
        "duration_seconds": elapsed,
        "frames_per_second": frames / elapsed if elapsed else 0,
        "slow_consumer_delay_ms": sse.slow_consumer_delay_ms,
        "reconnect_ratio_per_minute": sse.reconnect_ratio_per_minute,
        "frame_rate": frame_stats.as_dict(),
    }


async def _sample_prometheus(
    plan: CapacityPlan, report: CapacityReport, stop: asyncio.Event
) -> None:
    if plan.prometheus is None or not plan.prometheus.queries:
        return
    headers: dict[str, str] = {}
    if plan.prometheus.bearer_token_env:
        token = os.environ.get(plan.prometheus.bearer_token_env)
        if not token:
            report["blockers"].append(
                f"Environment variable {plan.prometheus.bearer_token_env!r} is required"
            )
            return
        headers["Authorization"] = f"Bearer {token}"
    samples: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        base_url=plan.prometheus.base_url.rstrip("/"),
        headers=headers,
        timeout=10.0,
        verify=True,
    ) as client:
        while not stop.is_set() and len(samples) < 10000:
            captured: dict[str, Any] = {"timestamp": datetime.now(UTC).isoformat()}
            for name, query in plan.prometheus.queries.items():
                try:
                    response = await client.get(
                        "/api/v1/query", params={"query": query}
                    )
                    response.raise_for_status()
                    payload = response.json()
                    captured[name] = payload.get("data", {}).get("result", [])
                except (httpx.HTTPError, ValueError, KeyError):
                    captured[name] = None
            samples.append(captured)
            try:
                await asyncio.wait_for(stop.wait(), timeout=5.0)
            except TimeoutError:
                pass
    report["prometheus_samples"] = samples


def _evaluate_acceptance(report: CapacityReport, target: AcceptanceTarget) -> None:
    metrics = report["metrics"]
    if report["scenario"] in {"agentscope_runs", "codex_runs"}:
        actual = float(metrics.get("peak_in_flight", 0))
    elif report["scenario"] == "event_store":
        actual = float(metrics.get("observed_events_per_second", 0))
    else:
        actual = float(metrics.get("peak_open_connections", 0))
    required = target.target * (1 + target.headroom_ratio)
    no_workload_failures = True
    if report["scenario"] in {"agentscope_runs", "codex_runs"}:
        no_workload_failures = metrics.get("failed_runs", 0) == 0
    elif report["scenario"] == "event_store":
        no_workload_failures = metrics.get("failed_batches", 0) == 0
    else:
        no_workload_failures = metrics.get("failed_connections", 0) == 0
    report["acceptance"] = [
        {
            "name": target.name,
            "actual": actual,
            "required": required,
            "target": target.target,
            "unit": target.unit,
            "headroom_ratio": target.headroom_ratio,
            "status": (
                "PASS"
                if actual >= required
                and no_workload_failures
                and not report["blockers"]
                else "FAIL"
            ),
        }
    ]

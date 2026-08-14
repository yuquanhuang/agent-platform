"""Validated, secret-free configuration for the capacity runner."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

ScenarioName = Literal[
    "agentscope_runs",
    "codex_runs",
    "event_store",
    "sse",
]
ENV_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
HEADER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
SENSITIVE_HEADER_NAMES = frozenset(
    {"authorization", "cookie", "proxy-authorization", "x-api-key", "x-auth-token"}
)
SENSITIVE_METADATA_PARTS = ("authorization", "cookie", "password", "secret", "token")


class AcceptanceTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    target: float = Field(gt=0)
    unit: str = Field(min_length=1, max_length=40)
    headroom_ratio: float = Field(default=0.30, ge=0, lt=1)


class RequestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(min_length=1, max_length=2048)
    auth_token_env: str | None = Field(default=None, min_length=1, max_length=128)
    request_timeout_seconds: float = Field(default=15.0, gt=0, le=300)
    verify_tls: bool = True
    static_headers: dict[str, str] = Field(default_factory=dict)
    secret_headers_env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_safe_references(self) -> RequestConfig:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain credentials, query or fragment")
        if self.auth_token_env and not ENV_NAME_PATTERN.fullmatch(self.auth_token_env):
            raise ValueError("auth_token_env must be an uppercase environment name")
        for header in self.static_headers:
            if not HEADER_NAME_PATTERN.fullmatch(header):
                raise ValueError("static header name is invalid")
            if header.lower() in SENSITIVE_HEADER_NAMES:
                raise ValueError(
                    f"sensitive header {header!r} must use secret_headers_env"
                )
        for header, env_name in self.secret_headers_env.items():
            if not HEADER_NAME_PATTERN.fullmatch(header):
                raise ValueError("secret header name is invalid")
            if not ENV_NAME_PATTERN.fullmatch(env_name):
                raise ValueError(
                    "secret_headers_env values must be uppercase environment names"
                )
        return self

    def headers(self) -> dict[str, str]:
        result = dict(self.static_headers)
        if self.auth_token_env:
            token = os.environ.get(self.auth_token_env)
            if not token:
                raise RuntimeError(
                    f"Environment variable {self.auth_token_env!r} is required"
                )
            result["Authorization"] = f"Bearer {token}"
        for header, env_name in self.secret_headers_env.items():
            value = os.environ.get(env_name)
            if not value:
                raise RuntimeError(f"Environment variable {env_name!r} is required")
            result[header] = value
        return result


class RunScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_ids: list[str] = Field(min_length=1, max_length=10000)
    deployment_id: str | None = None
    total_runs: int = Field(default=100, ge=1, le=100000)
    concurrency: int = Field(default=100, ge=1, le=10000)
    input_text: str = Field(
        default="capacity validation", min_length=1, max_length=100000
    )
    timeout_seconds: int = Field(default=600, ge=1, le=86400)
    poll_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    target: AcceptanceTarget | None = None

    @model_validator(mode="after")
    def validate_sessions(self) -> RunScenario:
        if self.concurrency > len(self.session_ids):
            raise ValueError("session_ids must cover the requested concurrency")
        return self


class EventTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=128)
    execution_attempt: int = Field(default=1, ge=1)
    fencing_token_env: str = Field(
        min_length=1, max_length=128, pattern=ENV_NAME_PATTERN.pattern
    )


class EventStoreScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targets: list[EventTarget] = Field(min_length=1, max_length=10000)
    events_per_second: float = Field(default=2000, gt=0, le=1000000)
    batch_size: int = Field(default=50, ge=1, le=200)
    duration_seconds: float = Field(default=900, gt=0, le=86400)
    target: AcceptanceTarget | None = None


class SseScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_ids: list[str] = Field(min_length=1, max_length=10000)
    connections: int = Field(default=1000, ge=1, le=10000)
    duration_seconds: float = Field(default=300, gt=0, le=86400)
    reconnect_ratio_per_minute: float = Field(default=0.05, ge=0, le=1)
    slow_consumer_delay_ms: int = Field(default=0, ge=0, le=60000)
    target: AcceptanceTarget | None = None


class PrometheusConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(min_length=1, max_length=2048)
    bearer_token_env: str | None = Field(default=None, min_length=1, max_length=128)
    queries: dict[str, str] = Field(default_factory=dict)


class CapacityPlan(BaseModel):
    """A single reproducible workload; secrets are referenced by env name only."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=1, max_length=128)
    scenario: ScenarioName
    request: RequestConfig
    run: RunScenario | None = None
    event_store: EventStoreScenario | None = None
    sse: SseScenario | None = None
    prometheus: PrometheusConfig | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    dry_run: bool = False

    @model_validator(mode="after")
    def validate_scenario(self) -> CapacityPlan:
        configured = {
            "agentscope_runs": self.run,
            "codex_runs": self.run,
            "event_store": self.event_store,
            "sse": self.sse,
        }
        selected = configured[self.scenario]
        if selected is None:
            raise ValueError(
                f"configuration for scenario {self.scenario!r} is required"
            )
        if self.scenario in {"agentscope_runs", "codex_runs"} and self.run is not None:
            default_target = 100 if self.scenario == "agentscope_runs" else 20
            if self.run.target is None:
                self.run.target = AcceptanceTarget(
                    name=f"{self.scenario}.concurrency",
                    target=default_target,
                    unit="concurrent_runs",
                )
        if (
            self.scenario == "event_store"
            and self.event_store is not None
            and self.event_store.target is None
        ):
            self.event_store.target = AcceptanceTarget(
                name="event_store.throughput",
                target=2000,
                unit="events_per_second",
            )
        if self.scenario == "sse" and self.sse is not None and self.sse.target is None:
            self.sse.target = AcceptanceTarget(
                name="sse.connections",
                target=1000,
                unit="connections",
            )
        for key in self.metadata:
            normalized = key.lower()
            if any(part in normalized for part in SENSITIVE_METADATA_PARTS):
                raise ValueError(
                    "metadata keys must not contain credential or secret fields"
                )
        return self


def load_plan(path: str | Path) -> CapacityPlan:
    """Load YAML or JSON and reject unknown fields before any network call."""

    plan_path = Path(path)
    payload = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("capacity plan must contain a mapping")
    return CapacityPlan.model_validate(payload)

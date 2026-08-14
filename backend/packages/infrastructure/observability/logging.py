"""Structured JSON logging with trusted correlation context."""

import json
import logging
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)
_tenant_id: ContextVar[str | None] = ContextVar("tenant_id", default=None)
_run_id: ContextVar[str | None] = ContextVar("run_id", default=None)
_workflow_id: ContextVar[str | None] = ContextVar("workflow_id", default=None)
_release_id: ContextVar[str | None] = ContextVar("release_id", default=None)


@contextmanager
def bind_log_context(
    *,
    request_id: str | None = None,
    trace_id: str | None = None,
    tenant_id: str | None = None,
    run_id: str | None = None,
    workflow_id: str | None = None,
    release_id: str | None = None,
) -> Generator[None, None, None]:
    """Bind safe correlation identifiers for the current async context."""

    tokens: list[tuple[ContextVar[str | None], Token[str | None]]] = []
    try:
        for variable, value in (
            (_request_id, request_id),
            (_trace_id, trace_id),
            (_tenant_id, tenant_id),
            (_run_id, run_id),
            (_workflow_id, workflow_id),
            (_release_id, release_id),
        ):
            if value is not None:
                tokens.append((variable, variable.set(value)))
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


class JsonLogFormatter(logging.Formatter):
    """Serialize stable, non-sensitive process and correlation fields."""

    def __init__(self, *, service_name: str, environment: str) -> None:
        super().__init__()
        self._service_name = service_name
        self._environment = environment

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self._service_name,
            "environment": self._environment,
        }
        for key, value in (
            ("request_id", _request_id.get()),
            ("trace_id", _trace_id.get()),
            ("tenant_id", _tenant_id.get()),
            ("run_id", _run_id.get()),
            ("workflow_id", _workflow_id.get()),
            ("release_id", _release_id.get()),
        ):
            if value is not None:
                payload[key] = value
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_json_logging(*, service_name: str, environment: str, level: str) -> None:
    """Install one JSON handler for the current backend process."""

    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonLogFormatter(service_name=service_name, environment=environment)
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

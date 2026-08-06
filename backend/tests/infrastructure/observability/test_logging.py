"""Structured logging correlation and safety tests."""

import json
import logging

from packages.infrastructure.observability.logging import (
    JsonLogFormatter,
    bind_log_context,
)


def test_json_log_contains_process_and_correlation_fields() -> None:
    formatter = JsonLogFormatter(service_name="api", environment="test")
    record = logging.LogRecord(
        name="agent.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="request completed",
        args=(),
        exc_info=None,
    )

    with bind_log_context(request_id="req-1", trace_id="trace-1"):
        payload = json.loads(formatter.format(record))

    assert payload["service"] == "api"
    assert payload["environment"] == "test"
    assert payload["request_id"] == "req-1"
    assert payload["trace_id"] == "trace-1"
    assert "authorization" not in payload

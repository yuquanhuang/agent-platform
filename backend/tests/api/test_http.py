"""HTTP middleware security regression tests."""

import logging

import pytest
from starlette.requests import Request

from apps.api.http import handle_unexpected_error


@pytest.mark.asyncio
async def test_unexpected_error_log_excludes_download_query_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = "sensitive-download-token-which-must-never-be-logged"
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": "/api/v1/artifact-downloads/grant-id",
            "raw_path": b"/api/v1/artifact-downloads/grant-id",
            "query_string": f"token={token}".encode(),
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 443),
        }
    )
    request.state.request_id = "req-download-log-redaction"

    with caplog.at_level(logging.ERROR, logger="apps.api.http"):
        response = await handle_unexpected_error(request, RuntimeError("failed"))

    assert response.status_code == 500
    assert "/api/v1/artifact-downloads/grant-id" in caplog.text
    assert token not in caplog.text
    assert "query_string" not in caplog.text

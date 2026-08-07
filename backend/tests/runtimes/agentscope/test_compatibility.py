from packages.runtimes.agentscope.compatibility import (
    AGENTSCOPE_LOCKED_VERSION,
    AGENTSCOPE_WHEEL_SHA256,
    probe_agentscope_compatibility,
)


def test_locked_agentscope_api_is_compatible() -> None:
    report = probe_agentscope_compatibility()

    assert report.version == AGENTSCOPE_LOCKED_VERSION == "2.0.5"
    assert report.python_version.startswith("3.12.")
    assert "yield_final_msg" in report.reply_stream_parameters
    assert "kwargs" in report.model_call_parameters
    assert "RequireExternalExecutionEvent" in report.event_types
    assert AGENTSCOPE_WHEEL_SHA256 == (
        "ae440075c8d72b21a0e6b54458b9c6b9d1d9594f1e3f885611df1ed93950d444"
    )

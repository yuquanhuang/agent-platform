"""Bounded Temporal client connection and namespace policy."""

import asyncio

from opentelemetry import trace
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.service import RetryConfig

from packages.infrastructure.config import AppSettings


def temporal_namespace(settings: AppSettings) -> str:
    return settings.temporal_namespace or f"agent-platform-{settings.env.value}"


async def connect_temporal_client(settings: AppSettings) -> Client:
    """Connect with bounded retry/timeout; absence never becomes fake readiness."""

    if settings.temporal_address is None:
        raise RuntimeError("AP_TEMPORAL_ADDRESS is required for Temporal processes")
    timeout_seconds = settings.temporal_connect_timeout_seconds
    return await asyncio.wait_for(
        Client.connect(
            settings.temporal_address,
            namespace=temporal_namespace(settings),
            data_converter=pydantic_data_converter,
            interceptors=[
                TracingInterceptor(tracer=trace.get_tracer("agent-platform.temporal"))
            ],
            retry_config=RetryConfig(
                max_elapsed_time_millis=int(timeout_seconds * 1_000),
                max_retries=3,
            ),
            identity=settings.service_name,
        ),
        timeout=timeout_seconds,
    )

"""Internal Prometheus endpoint."""

import ipaddress
from collections.abc import Sequence

from fastapi import APIRouter, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from packages.infrastructure.observability import PlatformMetrics


def create_metrics_router(
    metrics: PlatformMetrics, allowed_networks: Sequence[str]
) -> APIRouter:
    networks = tuple(
        ipaddress.ip_network(value, strict=False) for value in allowed_networks
    )
    router = APIRouter()

    async def metrics_endpoint(request: Request) -> Response:
        client_host = request.client.host if request.client is not None else None
        if client_host is None:
            return Response(status_code=403)
        try:
            client_ip = ipaddress.ip_address(client_host)
        except ValueError:
            return Response(status_code=403)
        if not any(client_ip in network for network in networks):
            return Response(status_code=403)
        return Response(
            content=generate_latest(metrics.registry),
            media_type=CONTENT_TYPE_LATEST,
        )

    router.add_api_route(
        "/metrics",
        metrics_endpoint,
        methods=["GET"],
        include_in_schema=False,
    )
    return router

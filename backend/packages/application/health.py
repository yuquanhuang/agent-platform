"""Application health service."""

from dataclasses import dataclass

from packages.contracts.public import HealthResponse, HealthStatus


@dataclass(frozen=True, slots=True)
class HealthService:
    service_name: str

    def live(self) -> HealthResponse:
        return HealthResponse(status=HealthStatus.OK, service_name=self.service_name)

    def ready(self) -> HealthResponse:
        return HealthResponse(status=HealthStatus.OK, service_name=self.service_name)

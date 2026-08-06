"""Request correlation metadata shared by application use cases."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RequestMetadata:
    request_id: str
    trace_id: str

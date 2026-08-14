"""Bounded latency and counter aggregation for capacity reports."""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class SampleStats:
    max_samples: int = 100_000
    values: list[float] = field(default_factory=list[float])
    count: int = 0
    errors: int = 0
    _seen: int = 0

    def observe(self, value: float, *, error: bool = False) -> None:
        if value < 0 or not math.isfinite(value):
            raise ValueError("sample must be a finite non-negative number")
        self.count += 1
        self._seen += 1
        if error:
            self.errors += 1
        if len(self.values) < self.max_samples:
            self.values.append(value)
        else:
            # Deterministic bounded sampling keeps report memory bounded.
            slot = self._seen % self.max_samples
            self.values[slot] = value

    @property
    def error_rate(self) -> float:
        return self.errors / self.count if self.count else 0.0

    def percentile(self, percentile: float) -> float | None:
        if not self.values:
            return None
        if not 0 <= percentile <= 100:
            raise ValueError("percentile must be between 0 and 100")
        ordered = sorted(self.values)
        index = max(0, math.ceil((percentile / 100) * len(ordered)) - 1)
        return ordered[index]

    def as_dict(self) -> dict[str, float | int | None]:
        return {
            "count": self.count,
            "errors": self.errors,
            "error_rate": self.error_rate,
            "p50_seconds": self.percentile(50),
            "p95_seconds": self.percentile(95),
            "p99_seconds": self.percentile(99),
            "max_seconds": max(self.values) if self.values else None,
            "sampled_values": len(self.values),
        }

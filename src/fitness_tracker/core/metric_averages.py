"""Running sensor means, with independent counts for asynchronous sensors."""

from dataclasses import dataclass, field
from math import isfinite


@dataclass
class MetricAverages:
    """Average actual readings without counting missing sensors or UI redraws."""

    _totals: dict[str, float] = field(default_factory=dict)
    _counts: dict[str, int] = field(default_factory=dict)

    def observe(self, **readings: float | None) -> None:
        """Include available, finite readings; zero remains a valid sample."""
        for name, value in readings.items():
            if value is None or not isfinite(value) or value < 0:
                continue
            self._totals[name] = self._totals.get(name, 0.0) + value
            self._counts[name] = self._counts.get(name, 0) + 1

    def values(self) -> dict[str, float]:
        """Return only metrics with samples in this averaging period."""
        return {name: total / self._counts[name] for name, total in self._totals.items()}

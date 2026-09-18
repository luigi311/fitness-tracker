"""Convert usable location fixes into independent outdoor sensor readings."""

from dataclasses import dataclass
from datetime import UTC, datetime

from fitness_tracker.hardware.location import LocationFix, haversine_distance_m

GPS_FIX_TIMEOUT_S = 10
_MAX_ACCURACY_M = 50


@dataclass(frozen=True)
class GpsMetrics:
    """Canonical measurements derived from one location fix."""

    speed_mps: float | None
    distance_m: float
    altitude_m: float | None


class GpsMetricProcessor:
    """Accumulate distance without joining stale, unordered or implausible fixes."""

    def __init__(self, max_speed_mps: float) -> None:
        self.max_speed_mps = max_speed_mps
        self.distance_m = 0.0
        self._previous: LocationFix | None = None
        self._timestamp_ms: int | None = None

    def break_segment(self) -> None:
        """Keep accumulated distance but require a fresh movement baseline."""
        self._previous = None
        self._timestamp_ms = None

    def process(self, fix: LocationFix, timestamp_ms: int) -> GpsMetrics | None:
        """Prefer provider speed, otherwise derive speed from consecutive positions."""
        if fix.accuracy_m is not None and fix.accuracy_m > _MAX_ACCURACY_M:
            return None
        if fix.source_time_utc is not None:
            age_s = (datetime.now(UTC) - fix.source_time_utc).total_seconds()
            if abs(age_s) > GPS_FIX_TIMEOUT_S:
                return None

        elapsed_s = self._segment_elapsed_s(fix, timestamp_ms)
        if elapsed_s is not None and elapsed_s <= 0:
            return None

        speed_mps = fix.speed_mps
        if speed_mps is not None and speed_mps > self.max_speed_mps:
            speed_mps = None
        if self._previous is not None and elapsed_s is not None:
            distance_m = haversine_distance_m(self._previous, fix)
            derived_speed = distance_m / elapsed_s
            if derived_speed > self.max_speed_mps:
                return None
            # A stationary provider reading is more reliable than position jitter.
            if speed_mps == 0:
                distance_m = 0.0
            self.distance_m += distance_m
            if speed_mps is None:
                speed_mps = derived_speed

        self._previous = fix
        self._timestamp_ms = timestamp_ms
        return GpsMetrics(speed_mps, self.distance_m, fix.altitude_m)

    def _segment_elapsed_s(self, fix: LocationFix, timestamp_ms: int) -> float | None:
        """Return a contiguous segment's duration, or None for a fresh baseline."""
        previous = self._previous
        if previous is None or self._timestamp_ms is None:
            return None
        elapsed_s = (timestamp_ms - self._timestamp_ms) / 1000.0
        if elapsed_s <= 0:
            return elapsed_s
        if elapsed_s > GPS_FIX_TIMEOUT_S:
            return None
        if previous.source_time_utc is not None and fix.source_time_utc is not None:
            elapsed_s = (fix.source_time_utc - previous.source_time_utc).total_seconds()
        return None if elapsed_s > GPS_FIX_TIMEOUT_S else elapsed_s

"""Convert usable location fixes into independent outdoor sensor readings."""

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from math import sqrt

from fitness_tracker.hardware.location import LocationFix, haversine_distance_m

GPS_FIX_TIMEOUT_S = 10
_MAX_ACCURACY_M = 50
_MIN_MOVING_SPEED_MPS = 0.3
_MIN_MOVEMENT_M = 3.0
_SPEED_WINDOW_S = 10.0


def _usable_fix(fix: LocationFix) -> bool:
    if fix.accuracy_m is not None and fix.accuracy_m > _MAX_ACCURACY_M:
        return False
    if fix.source_time_utc is not None:
        age_s = (datetime.now(UTC) - fix.source_time_utc).total_seconds()
        return abs(age_s) <= GPS_FIX_TIMEOUT_S
    return True


def _distance_to_segment_m(length: float, start_distance: float, end_distance: float) -> float:
    """Measure a fix's deviation from a short segment using its three side lengths."""
    if length == 0:
        return start_distance
    projection = (start_distance**2 + length**2 - end_distance**2) / (2 * length)
    if projection <= 0:
        return start_distance
    if projection >= length:
        return end_distance
    return sqrt(max(0.0, start_distance**2 - projection**2))


def _extreme_split(
    points: list[LocationFix],
    distances: list[list[float]],
    start: int,
    end: int,
) -> int | None:
    """Keep both ends of a confirmed excursion even when a window starts midway."""
    pair = (start, end)
    span = distances[start][end]
    for left in range(start, end):
        for right in range(left + 1, end + 1):
            distance = distances[left][right]
            if distance > span:
                pair, span = (left, right), distance
    uncertainty_m = max(
        _MIN_MOVEMENT_M,
        points[pair[0]].accuracy_m or 0.0,
        points[pair[1]].accuracy_m or 0.0,
    )
    if span > uncertainty_m:
        for index in pair:
            if start < index < end:
                return index
    return None


def _filtered_path_distance_m(points: list[LocationFix]) -> float:
    """Preserve turns while removing position deviations within reported accuracy."""
    distances = [[0.0] * len(points) for _point in points]
    for left, point in enumerate(points):
        for right in range(left + 1, len(points)):
            distances[left][right] = distances[right][left] = haversine_distance_m(
                point,
                points[right],
            )
    segments = [(0, len(points) - 1)]
    distance_m = 0.0
    while segments:
        start, end = segments.pop()
        first, last = points[start], points[end]
        split = _extreme_split(points, distances, start, end)
        if split is not None:
            segments.extend(((start, split), (split, end)))
            continue
        greatest_error = 1.0
        for index in range(start + 1, end):
            point = points[index]
            uncertainty_m = max(
                _MIN_MOVEMENT_M,
                first.accuracy_m or 0.0,
                point.accuracy_m or 0.0,
                last.accuracy_m or 0.0,
            )
            error = (
                _distance_to_segment_m(
                    distances[start][end],
                    distances[start][index],
                    distances[index][end],
                )
                / uncertainty_m
            )
            if error > greatest_error:
                split, greatest_error = index, error
        if split is None:
            distance_m += distances[start][end]
        else:
            segments.extend(((start, split), (split, end)))
    return distance_m


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
        self._movement_anchor: LocationFix | None = None
        self._movement_timestamp_ms: int | None = None
        self._speed_window: deque[tuple[LocationFix, int]] = deque()

    def break_segment(self) -> None:
        """Keep accumulated distance but require a fresh movement baseline."""
        self._previous = None
        self._timestamp_ms = None
        self._movement_anchor = None
        self._movement_timestamp_ms = None
        self._speed_window.clear()

    def process(self, fix: LocationFix, timestamp_ms: int) -> GpsMetrics | None:
        """Prefer provider speed, otherwise derive speed from consecutive positions."""
        if not _usable_fix(fix):
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
            if speed_mps is None:
                distance_m = self._derived_distance(fix, timestamp_ms)
            elif speed_mps < _MIN_MOVING_SPEED_MPS:
                distance_m = 0.0
                speed_mps = 0.0
                self._set_movement_anchor(fix, timestamp_ms)
            else:
                self._set_movement_anchor(fix, timestamp_ms)
            self.distance_m += distance_m
        else:
            self._speed_window.clear()
            self._set_movement_anchor(fix, timestamp_ms)
            if speed_mps is not None and speed_mps < _MIN_MOVING_SPEED_MPS:
                speed_mps = 0.0

        estimated_speed_mps = self._window_speed(fix, timestamp_ms)
        if speed_mps is None:
            speed_mps = estimated_speed_mps
        self._previous = fix
        self._timestamp_ms = timestamp_ms
        return GpsMetrics(speed_mps, self.distance_m, fix.altitude_m)

    def _set_movement_anchor(self, fix: LocationFix, timestamp_ms: int) -> None:
        self._movement_anchor = fix
        self._movement_timestamp_ms = timestamp_ms

    def _derived_distance(self, fix: LocationFix, timestamp_ms: int) -> float:
        """Wait for movement beyond the accuracy circles without discarding slow steps."""
        anchor = self._movement_anchor
        anchor_ms = self._movement_timestamp_ms
        if anchor is None or anchor_ms is None:
            self._set_movement_anchor(fix, timestamp_ms)
            return 0.0
        distance_m = haversine_distance_m(anchor, fix)
        uncertainty_m = max(
            _MIN_MOVEMENT_M,
            (anchor.accuracy_m or 0.0) + (fix.accuracy_m or 0.0),
        )
        if distance_m <= uncertainty_m:
            return 0.0
        elapsed_s = (timestamp_ms - anchor_ms) / 1000.0
        if anchor.source_time_utc is not None and fix.source_time_utc is not None:
            elapsed_s = (fix.source_time_utc - anchor.source_time_utc).total_seconds()
        self._set_movement_anchor(fix, timestamp_ms)
        speed_mps = distance_m / elapsed_s if elapsed_s > 0 else 0.0
        if not _MIN_MOVING_SPEED_MPS <= speed_mps <= self.max_speed_mps:
            return 0.0
        return distance_m

    def _window_speed(self, fix: LocationFix, timestamp_ms: int) -> float | None:
        """Estimate speed independently of distance-gate crossings over ten seconds."""
        self._speed_window.append((fix, timestamp_ms))
        while (
            len(self._speed_window) > 1
            and timestamp_ms - self._speed_window[1][1] >= _SPEED_WINDOW_S * 1000
        ):
            self._speed_window.popleft()
        first, first_ms = self._speed_window[0]
        elapsed_s = (timestamp_ms - first_ms) / 1000.0
        if first.source_time_utc is not None and fix.source_time_utc is not None:
            elapsed_s = (fix.source_time_utc - first.source_time_utc).total_seconds()
        if elapsed_s < _SPEED_WINDOW_S:
            return None
        distance_m = _filtered_path_distance_m([point for point, _timestamp in self._speed_window])
        speed_mps = distance_m / elapsed_s
        if speed_mps > self.max_speed_mps:
            return None
        return speed_mps if speed_mps >= _MIN_MOVING_SPEED_MPS else 0.0

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

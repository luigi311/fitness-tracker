"""Validated location types, recording policies, and pure filtering helpers."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from math import atan2, cos, isfinite, radians, sin, sqrt
from numbers import Real
from typing import Final, Protocol, Self

from fitness_tracker.core.environment import Environment
from fitness_tracker.core.sports import SportTypesEnum

_EARTH_RADIUS_M: Final = 6_371_008.8
_NANOSECONDS_PER_MILLISECOND: Final = 1_000_000
_MIN_LATITUDE_DEG: Final = -90.0
_MAX_LATITUDE_DEG: Final = 90.0
_MIN_LONGITUDE_DEG: Final = -180.0
_MAX_LONGITUDE_DEG: Final = 180.0
# Conservative upper bounds in metres per second for the pure spike filter helper.
_MAX_SPEED_MPS_BY_SPORT: Final[dict[SportTypesEnum, float]] = {
    SportTypesEnum.running: 12.0,
    SportTypesEnum.biking: 35.0,
    SportTypesEnum.unknown: 50.0,
}


class PortalAccuracy(IntEnum):
    """Accuracy levels accepted by the XDG Location Portal."""

    NONE = 0
    COUNTRY = 1
    CITY = 2
    NEIGHBORHOOD = 3
    STREET = 4
    EXACT = 5


_PORTAL_ACCURACY_BY_SETTING: Final[dict[str, PortalAccuracy]] = {
    "city": PortalAccuracy.CITY,
    "neighborhood": PortalAccuracy.NEIGHBORHOOD,
    "street": PortalAccuracy.STREET,
    "exact": PortalAccuracy.EXACT,
}


def portal_accuracy_for_setting(value: str) -> PortalAccuracy:
    """Convert a persisted location accuracy setting to its portal value."""
    try:
        return _PORTAL_ACCURACY_BY_SETTING[value]
    except KeyError as exc:
        message = f"Unknown indoor location accuracy setting: {value}"
        raise ValueError(message) from exc


class LocationState(StrEnum):
    """Nonfatal lifecycle states reported by a location source."""

    DISABLED = "disabled"
    STARTING = "starting"
    ACQUIRING = "acquiring"
    TRACKING = "tracking"
    UNAVAILABLE = "unavailable"
    DENIED = "denied"
    CANCELLED = "cancelled"
    ERROR = "error"
    STOPPED = "stopped"


def _finite_coordinate(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        message = f"{field_name} must be a finite real number"
        raise TypeError(message)
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        message = f"{field_name} must be a finite real number"
        raise ValueError(message) from exc
    if not isfinite(number):
        message = f"{field_name} must be a finite real number"
        raise ValueError(message)
    return number


def _optional_number(
    value: object,
    *,
    nonnegative: bool = False,
    upper_exclusive: float | None = None,
) -> float | None:
    """Normalize an optional numeric portal field without rejecting its position."""
    if value is None or isinstance(value, bool) or not isinstance(value, Real):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if not isfinite(number):
        return None
    if nonnegative and number < 0:
        return None
    if upper_exclusive is not None and number >= upper_exclusive:
        return None
    return number


def _optional_source_time(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class LocationFix:
    """One validated position supplied by a location provider."""

    latitude_deg: float
    longitude_deg: float
    accuracy_m: float | None = None
    altitude_m: float | None = None
    speed_mps: float | None = None
    heading_deg: float | None = None
    source_time_utc: datetime | None = None

    def __post_init__(self) -> None:
        latitude = _finite_coordinate(self.latitude_deg, "latitude_deg")
        longitude = _finite_coordinate(self.longitude_deg, "longitude_deg")
        if not _MIN_LATITUDE_DEG <= latitude <= _MAX_LATITUDE_DEG:
            message = "latitude_deg must be between -90 and 90"
            raise ValueError(message)
        if not _MIN_LONGITUDE_DEG <= longitude <= _MAX_LONGITUDE_DEG:
            message = "longitude_deg must be between -180 and 180"
            raise ValueError(message)

        object.__setattr__(self, "latitude_deg", latitude)
        object.__setattr__(self, "longitude_deg", longitude)
        object.__setattr__(
            self,
            "accuracy_m",
            _optional_number(self.accuracy_m, nonnegative=True),
        )
        object.__setattr__(self, "altitude_m", _optional_number(self.altitude_m))
        object.__setattr__(
            self,
            "speed_mps",
            _optional_number(self.speed_mps, nonnegative=True),
        )
        object.__setattr__(
            self,
            "heading_deg",
            _optional_number(self.heading_deg, nonnegative=True, upper_exclusive=360.0),
        )
        object.__setattr__(self, "source_time_utc", _optional_source_time(self.source_time_utc))


@dataclass(frozen=True, slots=True)
class LocationPolicy:
    """Portal request thresholds and the maximum accepted point count."""

    accuracy: PortalAccuracy
    time_threshold_s: int
    distance_threshold_m: int
    max_points: int | None = None

    def __post_init__(self) -> None:
        try:
            accuracy = PortalAccuracy(self.accuracy)
        except (TypeError, ValueError) as exc:
            message = "accuracy must be a valid PortalAccuracy"
            raise ValueError(message) from exc
        object.__setattr__(self, "accuracy", accuracy)

        for field_name in ("time_threshold_s", "distance_threshold_m"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                message = f"{field_name} must be a nonnegative integer"
                raise ValueError(message)
        if self.max_points is not None and (
            isinstance(self.max_points, bool)
            or not isinstance(self.max_points, int)
            or self.max_points < 1
        ):
            message = "max_points must be a positive integer or None"
            raise ValueError(message)

    @classmethod
    def outdoor(cls) -> Self:
        """Return the initial policy for an outdoor route."""
        return cls(
            accuracy=PortalAccuracy.EXACT,
            time_threshold_s=1,
            distance_threshold_m=3,
        )

    @classmethod
    def anchor(cls, accuracy: PortalAccuracy = PortalAccuracy.NEIGHBORHOOD) -> Self:
        """Return the initial one-point policy for an indoor or trainer anchor."""
        return cls(
            accuracy=accuracy,
            time_threshold_s=0,
            distance_threshold_m=0,
            max_points=1,
        )


def location_policy_for_environment(
    environment: Environment,
    *,
    record_outdoor_routes: bool = True,
    record_indoor_anchor: bool = False,
    indoor_accuracy: PortalAccuracy = PortalAccuracy.NEIGHBORHOOD,
) -> LocationPolicy | None:
    """Resolve the configured location policy for one recording environment."""
    environment = Environment(environment)
    if environment is Environment.OUTDOOR:
        return LocationPolicy.outdoor() if record_outdoor_routes else None
    if record_indoor_anchor:
        return LocationPolicy.anchor(indoor_accuracy)
    return None


def max_speed_mps_for_sport(sport_type: SportTypesEnum) -> float:
    """Return the conservative spike-filter speed limit for a sport."""
    return _MAX_SPEED_MPS_BY_SPORT[SportTypesEnum(sport_type)]


LocationFixCallback = Callable[[LocationFix], None]
LocationStateCallback = Callable[[LocationState, str | None], None]


class LocationSource(Protocol):
    """Async source boundary used by the recorder."""

    async def start(
        self,
        policy: LocationPolicy,
        on_fix: LocationFixCallback,
        on_state: LocationStateCallback,
    ) -> None:
        """Start delivering fixes and source state transitions."""

    async def stop(self) -> None:
        """Stop delivery and release source resources."""


def relative_timestamp_ms(recording_origin_ns: int, receipt_ns: int | None = None) -> int:
    """Return a nonnegative timestamp relative to a monotonic recording origin."""
    if isinstance(recording_origin_ns, bool) or not isinstance(recording_origin_ns, int):
        message = "recording_origin_ns must be an integer"
        raise TypeError(message)
    if receipt_ns is None:
        receipt_ns = time.monotonic_ns()
    if isinstance(receipt_ns, bool) or not isinstance(receipt_ns, int):
        message = "receipt_ns must be an integer"
        raise TypeError(message)
    return max(0, (receipt_ns - recording_origin_ns) // _NANOSECONDS_PER_MILLISECOND)


def haversine_distance_m(first: LocationFix, second: LocationFix) -> float:
    """Return the great-circle distance between two validated positions in metres."""
    delta_latitude = radians(second.latitude_deg - first.latitude_deg)
    raw_delta_longitude = radians(second.longitude_deg - first.longitude_deg)
    delta_longitude = atan2(sin(raw_delta_longitude), cos(raw_delta_longitude))
    first_latitude = radians(first.latitude_deg)
    second_latitude = radians(second.latitude_deg)
    haversine = (
        sin(delta_latitude / 2.0) ** 2
        + cos(first_latitude) * cos(second_latitude) * sin(delta_longitude / 2.0) ** 2
    )
    haversine = min(1.0, max(0.0, haversine))
    return 2.0 * _EARTH_RADIUS_M * atan2(sqrt(haversine), sqrt(1.0 - haversine))


def is_plausible_motion(
    previous: LocationFix,
    current: LocationFix,
    elapsed_ms: int,
    *,
    max_speed_mps: float,
) -> bool:
    """Return whether a position change is plausible for the selected sport.

    Circle overlap is considered only when both fixes report accuracy. A fast
    segment with incomplete accuracy data is preserved.
    """
    if isinstance(elapsed_ms, bool) or not isinstance(elapsed_ms, int) or elapsed_ms < 0:
        message = "elapsed_ms must be a nonnegative integer"
        raise ValueError(message)
    if isinstance(max_speed_mps, bool) or not isinstance(max_speed_mps, Real):
        message = "max_speed_mps must be a finite positive number"
        raise TypeError(message)
    max_speed = float(max_speed_mps)
    if not isfinite(max_speed) or max_speed <= 0:
        message = "max_speed_mps must be a finite positive number"
        raise ValueError(message)

    distance_m = haversine_distance_m(previous, current)
    if elapsed_ms == 0:
        return True
    implied_speed_mps = distance_m / (elapsed_ms / 1_000.0)
    if implied_speed_mps <= max_speed:
        return True
    if previous.accuracy_m is None or current.accuracy_m is None:
        return True
    return distance_m <= previous.accuracy_m + current.accuracy_m


@dataclass(slots=True)
class LocationFilter:
    """Accept valid, ordered fixes according to one recording policy."""

    policy: LocationPolicy
    max_speed_mps: float
    _accepted_count: int = field(default=0, init=False)
    _last_fix: LocationFix | None = field(default=None, init=False)
    _last_timestamp_ms: int | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if isinstance(self.max_speed_mps, bool) or not isinstance(self.max_speed_mps, Real):
            message = "max_speed_mps must be a finite positive number"
            raise TypeError(message)
        max_speed = float(self.max_speed_mps)
        if not isfinite(max_speed) or max_speed <= 0:
            message = "max_speed_mps must be a finite positive number"
            raise ValueError(message)
        self.max_speed_mps = max_speed

    @property
    def accepted_count(self) -> int:
        """Return the number of accepted fixes in this recording."""
        return self._accepted_count

    @property
    def last_fix(self) -> LocationFix | None:
        """Return the most recently accepted fix, if any."""
        return self._last_fix

    @property
    def last_timestamp_ms(self) -> int | None:
        """Return the timestamp of the most recently accepted fix, if any."""
        return self._last_timestamp_ms

    def accept(self, fix: LocationFix, timestamp_ms: int) -> LocationFix | None:
        """Return a fix when it passes validation, ordering, and policy limits."""
        if not isinstance(fix, LocationFix):
            message = "fix must be a LocationFix"
            raise TypeError(message)
        if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int):
            message = "timestamp_ms must be an integer"
            raise TypeError(message)
        if timestamp_ms < 0:
            return None
        if self.policy.max_points is not None and self._accepted_count >= self.policy.max_points:
            return None
        last_timestamp_ms = self._last_timestamp_ms
        if last_timestamp_ms is not None and timestamp_ms < last_timestamp_ms:
            return None
        if self._last_fix is not None:
            same_position = (
                fix.latitude_deg == self._last_fix.latitude_deg
                and fix.longitude_deg == self._last_fix.longitude_deg
            )
            if same_position and fix.source_time_utc == self._last_fix.source_time_utc:
                return None
            if last_timestamp_ms is not None and not is_plausible_motion(
                self._last_fix,
                fix,
                timestamp_ms - last_timestamp_ms,
                max_speed_mps=self.max_speed_mps,
            ):
                return None

        self._accepted_count += 1
        self._last_fix = fix
        self._last_timestamp_ms = timestamp_ms
        return fix

    def reset(self) -> None:
        """Clear accepted-fix state before reusing the filter for another recording."""
        self._accepted_count = 0
        self._last_fix = None
        self._last_timestamp_ms = None

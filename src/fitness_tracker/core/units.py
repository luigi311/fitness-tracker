"""Unit conversions and display formatting.

The functions in this module operate on the application's canonical units:
metres, metres per second, and seconds.  They intentionally have no GTK,
database, or hardware dependencies so callers can use the same rules in
views, simulations, and integrations.
"""

from __future__ import annotations

from enum import StrEnum
from math import inf, isfinite
from typing import Final, Literal

from fitness_tracker.core.sports import SportTypesEnum


class UnitSystem(StrEnum):
    """Distance and speed units used for display."""

    METRIC = "metric"
    IMPERIAL = "imperial"


class DurationStyle(StrEnum):
    """Supported clock display styles."""

    CLOCK = "clock"
    COUNTDOWN = "countdown"


UnitQuantity = Literal["distance", "pace", "speed"]
UnitSystemLike = UnitSystem | str
DurationStyleLike = DurationStyle | str

MPS_TO_MPH: Final = 2.236936
MPS_TO_KPH: Final = 3.6
M_TO_MI: Final = 0.000621371
M_TO_KM: Final = 0.001
MPH_TO_MPS: Final = 0.44704
MPH_TO_KPH: Final = 1.60934

_DISTANCE_KM_THRESHOLD_M: Final = 1000.0
_DURATION_INTEGER_TOLERANCE: Final = 1e-6
_PACE_MIN_SPEED_MPS: Final = 0.01
_SECONDS_PER_MINUTE: Final = 60


def _unit_system(system: UnitSystemLike) -> UnitSystem:
    try:
        return UnitSystem(system)
    except ValueError:
        msg = f"Unknown unit system: {system!r}"
        raise ValueError(msg) from None


def _duration_style(style: DurationStyleLike) -> DurationStyle:
    try:
        return DurationStyle(style)
    except ValueError:
        msg = f"Unknown duration style: {style!r}"
        raise ValueError(msg) from None


def unit_label(quantity: UnitQuantity, system: UnitSystemLike) -> str:
    """Return the short display label for a quantity in ``system``."""
    resolved_system = _unit_system(system)
    if quantity == "speed":
        return "mph" if resolved_system == UnitSystem.IMPERIAL else "km/h"
    if quantity in ("distance", "pace"):
        return "mi" if resolved_system == UnitSystem.IMPERIAL else "km"
    msg = f"Unknown unit quantity: {quantity!r}"
    raise ValueError(msg)


def speed_in_units(mps: float, system: UnitSystemLike) -> float:
    """Convert metres per second to the display speed unit."""
    resolved_system = _unit_system(system)
    factor = MPS_TO_MPH if resolved_system == UnitSystem.IMPERIAL else MPS_TO_KPH
    return float(mps) * factor


def distance_in_units(meters: float, system: UnitSystemLike) -> float:
    """Convert metres to the display distance unit."""
    resolved_system = _unit_system(system)
    factor = M_TO_MI if resolved_system == UnitSystem.IMPERIAL else M_TO_KM
    return float(meters) * factor


def mps_from_mph(mph: float) -> float:
    """Convert miles per hour to metres per second."""
    return float(mph) * MPH_TO_MPS


def kph_from_mph(mph: float) -> float:
    """Convert miles per hour to kilometres per hour."""
    return float(mph) * MPH_TO_KPH


def pace_minutes_per_unit(mps: float, system: UnitSystemLike) -> float:
    """Return minutes per displayed distance unit, or infinity below threshold."""
    value = float(mps)
    if not isfinite(value) or value <= _PACE_MIN_SPEED_MPS:
        return inf
    return 60.0 / speed_in_units(value, system)


def format_pace(
    mps: float,
    system: UnitSystemLike,
    *,
    empty: str = "0:00",
    include_unit: bool = False,
) -> str:
    """Format metres per second as a rounded ``minutes:seconds`` pace.

    ``empty`` controls the display for a stopped or unavailable speed.  The
    default is used by the live tracker; history passes ``"—"``.  The unit is
    omitted by default because metric widgets already render it separately.
    """
    resolved_system = _unit_system(system)
    mins_per_unit = pace_minutes_per_unit(mps, resolved_system)
    if not isfinite(mins_per_unit):
        return empty

    minutes = int(mins_per_unit)
    seconds = round((mins_per_unit - minutes) * _SECONDS_PER_MINUTE)
    if seconds >= _SECONDS_PER_MINUTE:
        minutes += seconds // _SECONDS_PER_MINUTE
        seconds %= _SECONDS_PER_MINUTE
    value = f"{minutes}:{seconds:02d}"
    if include_unit:
        return f"{value} min/{unit_label('pace', resolved_system)}"
    return value


def format_speed(
    mps: float,
    system: UnitSystemLike,
    *,
    include_unit: bool = False,
) -> str:
    """Format metres per second to one decimal display speed."""
    resolved_system = _unit_system(system)
    value = f"{speed_in_units(mps, resolved_system):.1f}"
    if include_unit:
        return f"{value} {unit_label('speed', resolved_system)}"
    return value


def _format_number(value: float) -> str:
    """Format a measurement without unnecessary trailing zeroes."""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def format_distance(
    meters: float | None,
    system: UnitSystemLike,
    *,
    include_whole_km_decimal: bool = False,
    empty: str | None = None,
) -> str:
    """Format metres using the requested distance system.

    Metric distances below one kilometre are shown in metres; larger values
    are shown in kilometres.  Imperial distances use miles with two decimal
    places.  ``empty`` lets history distinguish unavailable distance from a
    real zero-valued metric.
    """
    resolved_system = _unit_system(system)
    if meters is None or not isfinite(float(meters)) or float(meters) < 0:
        if empty is not None:
            return empty
        return "0 m" if resolved_system == UnitSystem.METRIC else "0.00 mi"

    meters_value = float(meters)
    if meters_value == 0 and empty is not None:
        return empty
    if resolved_system == UnitSystem.IMPERIAL:
        return f"{distance_in_units(meters_value, resolved_system):.2f} mi"

    if meters_value < _DISTANCE_KM_THRESHOLD_M:
        return f"{_format_number(meters_value)} m"
    kilometers = _format_number(distance_in_units(meters_value, resolved_system))
    if include_whole_km_decimal and "." not in kilometers:
        kilometers += ".0"
    return f"{kilometers} km"


def format_duration(
    seconds: float,
    style: DurationStyleLike,
    *,
    always_hours: bool | None = None,
    pad_hours: bool = True,
    pad_minutes: bool = True,
) -> str:
    """Format seconds as either a clock or an unbounded countdown.

    A clock defaults to ``HH:MM:SS``.  The optional padding controls preserve
    the compact history and workout elapsed forms while their callers migrate
    to this single implementation.  A countdown is always ``MM:SS`` with
    minutes allowed to exceed 59.
    """
    resolved_style = _duration_style(style)
    total_seconds = max(0, int(float(seconds)))
    if resolved_style == DurationStyle.COUNTDOWN:
        minutes, remainder = divmod(total_seconds, _SECONDS_PER_MINUTE)
        minutes_text = f"{minutes:02d}" if pad_minutes else f"{minutes:d}"
        return f"{minutes_text}:{remainder:02d}"

    if always_hours is None:
        always_hours = True
    hours, remainder = divmod(total_seconds, 3600)
    minutes, remainder = divmod(remainder, _SECONDS_PER_MINUTE)
    if always_hours or hours:
        hours_text = f"{hours:02d}" if pad_hours else f"{hours:d}"
        return f"{hours_text}:{minutes:02d}:{remainder:02d}"
    minutes_text = f"{minutes:02d}" if pad_minutes else f"{minutes:d}"
    return f"{minutes_text}:{remainder:02d}"


def format_human_duration(seconds: float) -> str:
    """Format a duration as compact human-readable minutes and seconds."""
    seconds = max(0.0, float(seconds))
    rounded_seconds = round(seconds)
    if abs(seconds - rounded_seconds) < _DURATION_INTEGER_TOLERANCE:
        total_seconds = int(rounded_seconds)
        minutes, remainder = divmod(total_seconds, _SECONDS_PER_MINUTE)
        if minutes and remainder:
            return f"{minutes} min {remainder} s"
        if minutes:
            return f"{minutes} min"
        return f"{remainder} s"
    return f"{seconds:.1f} s"


def display_cadence(value: float, sport: SportTypesEnum) -> int:
    """Convert running stride cadence to displayed steps per minute."""
    return int(float(value) * 2) if sport == SportTypesEnum.running else int(value)

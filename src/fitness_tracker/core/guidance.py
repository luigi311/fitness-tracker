"""Pure workout-target resolution and display guidance."""

from collections.abc import Mapping
from enum import IntEnum, StrEnum
from typing import Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, model_validator

from fitness_tracker.core.units import (
    UnitSystem,
    format_distance,
    format_human_duration,
    format_pace,
    unit_label,
)


class TargetDomain(StrEnum):
    """The target domain selected for a workout step."""

    NONE = "none"
    POWER = "power"
    PACE = "pace"
    HEART_RATE = "heart_rate"


class PebbleTargetKind(IntEnum):
    """Wire values used to describe the active workout target to Pebble."""

    NONE = 0
    POWER = 1
    PACE = 2
    HEART_RATE = 3


class StepGuidance(BaseModel):
    """Resolved target values and display text for one workout snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    domain: TargetDomain
    low: float
    mid: float
    high: float
    target_text: str
    next_text: str
    pebble_kind: PebbleTargetKind

    @model_validator(mode="after")
    def _validate_band(self) -> Self:
        if not self.low <= self.mid <= self.high:
            message = "guidance target values must be ordered low <= mid <= high"
            raise ValueError(message)
        return self


@runtime_checkable
class _PointTarget(Protocol):
    @property
    def value(self) -> float: ...


@runtime_checkable
class _RangeTarget(Protocol):
    @property
    def low(self) -> float: ...

    @property
    def high(self) -> float: ...


@runtime_checkable
class _RampTarget(Protocol):
    @property
    def start(self) -> float: ...

    @property
    def end(self) -> float: ...


class _WorkoutStep(Protocol):
    @property
    def duration(self) -> object: ...

    @property
    def power_watts(self) -> object | None: ...

    @property
    def speed_mps(self) -> object | None: ...

    @property
    def heart_rate_bpm(self) -> object | None: ...

    @property
    def heart_rate_percent_max(self) -> object | None: ...

    @property
    def heart_rate_percent_lthr(self) -> object | None: ...

    @property
    def heart_rate_zone(self) -> object | None: ...


class _HeartRateSettings(Protocol):
    @property
    def max_hr(self) -> float: ...

    @property
    def lthr_bpm(self) -> float | None: ...


@runtime_checkable
class _TimeDuration(Protocol):
    @property
    def seconds(self) -> float: ...


@runtime_checkable
class _DistanceDuration(Protocol):
    @property
    def meters(self) -> float: ...


@runtime_checkable
class _OpenDuration(Protocol):
    @property
    def event(self) -> str: ...


def apply_target_bias(
    values: tuple[float, float, float] | None,
    percent: int,
    decimal_places: int | None = None,
) -> tuple[float, float, float] | None:
    """Scale a resolved workout target by the trainer bias percentage."""
    if values is None:
        return None
    factor = 1.0 + percent / 100.0
    low, current, high = values
    adjusted = low * factor, current * factor, high * factor
    if decimal_places is None:
        return adjusted
    return (
        round(adjusted[0], decimal_places),
        round(adjusted[1], decimal_places),
        round(adjusted[2], decimal_places),
    )


def resolve_target_values(
    target: object | None,
    progress: float = 0.0,
    *,
    bias_pct: int = 0,
    decimal_places: int | None = None,
) -> tuple[float, float, float] | None:
    """Resolve a point, range, or ramp target and apply trainer bias."""
    values = _target_values(target, progress)
    return apply_target_bias(values, bias_pct, decimal_places)


def resolve_heart_rate_target(
    step: _WorkoutStep,
    progress: float = 0.0,
    *,
    bias_pct: int = 0,
    personal: _HeartRateSettings,
    zones: Mapping[str, tuple[float, float]],
) -> tuple[float, float, float] | None:
    """Resolve a step's preferred heart-rate target to an absolute BPM band."""
    absolute = _target_values(step.heart_rate_bpm, progress)
    if absolute is not None:
        return apply_target_bias(absolute, bias_pct, 0)

    percent_max = _target_values(step.heart_rate_percent_max, progress)
    if percent_max is not None:
        factor = float(personal.max_hr) / 100.0
        values = (
            percent_max[0] * factor,
            percent_max[1] * factor,
            percent_max[2] * factor,
        )
    else:
        percent_lthr = _target_values(step.heart_rate_percent_lthr, progress)
        if percent_lthr is not None and personal.lthr_bpm:
            factor = float(personal.lthr_bpm) / 100.0
            values = (
                percent_lthr[0] * factor,
                percent_lthr[1] * factor,
                percent_lthr[2] * factor,
            )
        else:
            values = _resolve_zone_target(step.heart_rate_zone, progress, zones)
    return apply_target_bias(values, bias_pct, 0)


def format_target_band(
    domain: TargetDomain,
    low: float,
    high: float,
    *,
    prefix: str,
    suffix: str = "",
    unit_system: UnitSystem = UnitSystem.IMPERIAL,
) -> str:
    """Format the low/high values for a target or next-step label."""
    if domain is TargetDomain.POWER:
        return f"{prefix}{round(low)} - {round(high)} W{suffix}"
    if domain is TargetDomain.PACE:
        low_pace = format_pace(low, unit_system)
        high_pace = format_pace(high, unit_system)
        return f"{prefix}{high_pace} - {low_pace} /{unit_label('pace', unit_system)}{suffix}"
    if domain is TargetDomain.HEART_RATE:
        return f"{prefix}{round(low)} - {round(high)} bpm{suffix}"
    return f"{prefix}—"


def resolve_step_guidance(
    step: _WorkoutStep,
    next_step: _WorkoutStep | None,
    *,
    progress: float = 0.0,
    bias_pct: int = 0,
    personal: _HeartRateSettings,
    zones: Mapping[str, tuple[float, float]],
    unit_system: UnitSystem = UnitSystem.IMPERIAL,
) -> StepGuidance:
    """Resolve one active step and its next-step preview without UI dependencies."""
    power = resolve_target_values(
        step.power_watts,
        progress,
        bias_pct=bias_pct,
        decimal_places=0,
    )
    speed = resolve_target_values(
        step.speed_mps,
        progress,
        bias_pct=bias_pct,
        decimal_places=1,
    )
    heart_rate = resolve_heart_rate_target(
        step,
        progress,
        bias_pct=bias_pct,
        personal=personal,
        zones=zones,
    )
    domain, values = _select_domain(power, speed, heart_rate)
    next_domain, next_values = _select_next_domain(
        next_step,
        bias_pct=bias_pct,
        personal=personal,
        zones=zones,
    )
    next_suffix = ""
    if next_step is not None:
        next_suffix = f" for {format_step_duration(next_step)}"

    return StepGuidance(
        domain=domain,
        low=values[0],
        mid=values[1],
        high=values[2],
        target_text=format_target_band(
            domain,
            values[0],
            values[2],
            unit_system=unit_system,
            prefix="Target: ",
        ),
        next_text=format_target_band(
            next_domain,
            next_values[0],
            next_values[2],
            unit_system=unit_system,
            prefix="Next: ",
            suffix=next_suffix,
        ),
        pebble_kind=_pebble_kind(domain),
    )


def _target_values(
    target: object | None,
    progress: float,
) -> tuple[float, float, float] | None:
    if target is None:
        return None
    if isinstance(target, _PointTarget):
        value = float(target.value)
        return value, value, value
    if isinstance(target, _RangeTarget):
        low = float(target.low)
        high = float(target.high)
        return low, (low + high) / 2.0, high
    if isinstance(target, _RampTarget):
        bounded_progress = min(1.0, max(0.0, progress))
        current = float(target.start) + (float(target.end) - float(target.start)) * bounded_progress
        return current, current, current
    return None


def _resolve_zone_target(
    target: object | None,
    progress: float,
    zones: Mapping[str, tuple[float, float]],
) -> tuple[float, float, float] | None:
    if target is None:
        return None

    zone_values = tuple(zones.values())
    if not zone_values:
        return None

    def zone_bounds(value: float) -> tuple[float, float]:
        index = min(len(zone_values), max(1, round(value))) - 1
        low, high = zone_values[index]
        return float(low), float(high)

    if isinstance(target, _PointTarget):
        low, high = zone_bounds(float(target.value))
    elif isinstance(target, _RangeTarget):
        low = zone_bounds(float(target.low))[0]
        high = zone_bounds(float(target.high))[1]
    elif isinstance(target, _RampTarget):
        bounded_progress = min(1.0, max(0.0, progress))
        zone = float(target.start) + (float(target.end) - float(target.start)) * bounded_progress
        low, high = zone_bounds(zone)
    else:
        return None
    return low, (low + high) / 2.0, high


def _select_domain(
    power: tuple[float, float, float] | None,
    speed: tuple[float, float, float] | None,
    heart_rate: tuple[float, float, float] | None,
) -> tuple[TargetDomain, tuple[float, float, float]]:
    if power is not None:
        return TargetDomain.POWER, power
    if speed is not None:
        return TargetDomain.PACE, speed
    if heart_rate is not None:
        return TargetDomain.HEART_RATE, heart_rate
    return TargetDomain.NONE, (0.0, 0.0, 0.0)


def _select_next_domain(
    step: _WorkoutStep | None,
    *,
    bias_pct: int,
    personal: _HeartRateSettings,
    zones: Mapping[str, tuple[float, float]],
) -> tuple[TargetDomain, tuple[float, float, float]]:
    if step is None:
        return TargetDomain.NONE, (0.0, 0.0, 0.0)
    power = resolve_target_values(step.power_watts, bias_pct=bias_pct, decimal_places=0)
    speed = resolve_target_values(step.speed_mps, bias_pct=bias_pct, decimal_places=1)
    heart_rate = resolve_heart_rate_target(
        step,
        bias_pct=bias_pct,
        personal=personal,
        zones=zones,
    )
    return _select_domain(power, speed, heart_rate)


def _pebble_kind(domain: TargetDomain) -> PebbleTargetKind:
    match domain:
        case TargetDomain.NONE:
            return PebbleTargetKind.NONE
        case TargetDomain.POWER:
            return PebbleTargetKind.POWER
        case TargetDomain.PACE:
            return PebbleTargetKind.PACE
        case TargetDomain.HEART_RATE:
            return PebbleTargetKind.HEART_RATE


def format_step_duration(step: _WorkoutStep) -> str:
    """Return a workout step duration in compact display form."""
    duration = step.duration
    if isinstance(duration, _TimeDuration):
        result = format_human_duration(duration.seconds)
    elif isinstance(duration, _DistanceDuration):
        result = format_distance(
            float(duration.meters),
            UnitSystem.METRIC,
            include_whole_km_decimal=False,
        )
    elif isinstance(duration, _OpenDuration):
        result = "Open"
    else:
        result = str(duration)
    return result

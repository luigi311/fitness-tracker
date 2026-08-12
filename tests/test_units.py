"""Durable formatting contracts shared by tracker, history, and workouts."""

from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.core.units import (
    MPS_TO_MPH,
    DurationStyle,
    UnitSystem,
    display_cadence,
    format_distance,
    format_duration,
    format_human_duration,
    format_pace,
    mps_from_mph,
)

STRIDE_CADENCE = 85
RUNNING_STEP_CADENCE = 170


def test_pace_formatting_handles_empty_values_and_minute_rounding() -> None:
    carry_mph = 60.0 / (5.0 + 59.5 / 60.0)
    carry_mps = carry_mph / MPS_TO_MPH

    assert format_pace(0.0, UnitSystem.IMPERIAL) == "0:00"
    assert format_pace(mps_from_mph(carry_mph), UnitSystem.IMPERIAL) == "6:00"
    assert (
        format_pace(carry_mps, UnitSystem.IMPERIAL, empty="—", include_unit=True)
        == "6:00 min/mi"
    )
    assert format_pace(0.0, UnitSystem.IMPERIAL, empty="—") == "—"


def test_duration_styles_keep_clock_and_countdown_semantics() -> None:
    assert format_duration(0, DurationStyle.CLOCK, always_hours=False) == "00:00"
    assert format_duration(3_661, DurationStyle.CLOCK, always_hours=False) == "01:01:01"
    assert (
        format_duration(
            3_661,
            DurationStyle.CLOCK,
            always_hours=False,
            pad_hours=False,
            pad_minutes=False,
        )
        == "1:01:01"
    )
    assert format_duration(3_661, DurationStyle.COUNTDOWN) == "61:01"


def test_distance_formatting_switches_units_at_one_kilometre() -> None:
    assert format_distance(999.9, UnitSystem.METRIC) == "999.9 m"
    assert format_distance(1_000.0, UnitSystem.METRIC) == "1 km"
    assert (
        format_distance(1_000.0, UnitSystem.METRIC, include_whole_km_decimal=True)
        == "1.0 km"
    )
    assert format_distance(1_234.56, UnitSystem.IMPERIAL, empty="—") == "0.77 mi"


def test_running_cadence_converts_strides_to_steps() -> None:
    assert display_cadence(STRIDE_CADENCE, SportTypesEnum.running) == RUNNING_STEP_CADENCE
    assert display_cadence(STRIDE_CADENCE, SportTypesEnum.biking) == STRIDE_CADENCE
    assert display_cadence(STRIDE_CADENCE, SportTypesEnum.unknown) == STRIDE_CADENCE


def test_human_duration_keeps_significant_time_units() -> None:
    assert format_human_duration(59.4) == "59.4 s"
    assert format_human_duration(60.0) == "1 min"
    assert format_human_duration(61.0) == "1 min 1 s"
    assert format_human_duration(120.0) == "2 min"

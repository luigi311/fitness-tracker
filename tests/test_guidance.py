import pytest
from fitness_tracker.core.guidance import (
    PebbleTargetKind,
    TargetDomain,
    resolve_heart_rate_target,
    resolve_step_guidance,
)
from fitness_tracker.core.settings import PersonalSettings
from fitness_tracker.core.units import UnitSystem
from fitness_tracker.core.zones import HeartRateZones, ZoneThresholds
from workout_parser import DistanceDuration, PointTarget, RampTarget, RangeTarget, WorkoutStep


def _personal() -> PersonalSettings:
    return PersonalSettings(resting_hr=60, max_hr=190, lthr_bpm=150)


def _zones() -> ZoneThresholds:
    return HeartRateZones(resting_hr=60, max_hr=190).thresholds


def test_heart_rate_target_precedence() -> None:
    step = WorkoutStep(
        duration=DistanceDuration(meters=100),
        heart_rate_bpm=PointTarget(value=120),
        heart_rate_percent_max=PointTarget(value=80),
        heart_rate_percent_lthr=PointTarget(value=90),
        heart_rate_zone=PointTarget(value=2),
    )

    assert resolve_heart_rate_target(step, personal=_personal(), zones=_zones()) == (
        120,
        120,
        120,
    )

    step = step.model_copy(update={"heart_rate_bpm": None})
    assert resolve_heart_rate_target(step, personal=_personal(), zones=_zones()) == (
        152,
        152,
        152,
    )

    step = step.model_copy(update={"heart_rate_percent_max": None})
    assert resolve_heart_rate_target(step, personal=_personal(), zones=_zones()) == (
        135,
        135,
        135,
    )

    step = step.model_copy(update={"heart_rate_percent_lthr": None})
    assert resolve_heart_rate_target(step, personal=_personal(), zones=_zones()) == (
        138,
        144,
        151,
    )


def test_power_guidance_resolves_bias_and_next_preview() -> None:
    step = WorkoutStep(
        duration=DistanceDuration(meters=1000),
        power_watts=RangeTarget(low=200, high=220),
    )
    next_step = WorkoutStep(
        duration=DistanceDuration(meters=500),
        heart_rate_zone=PointTarget(value=3),
    )

    guidance = resolve_step_guidance(
        step,
        next_step,
        bias_pct=10,
        personal=_personal(),
        zones=_zones(),
        unit_system=UnitSystem.METRIC,
    )

    assert guidance.domain is TargetDomain.POWER
    assert guidance.pebble_kind is PebbleTargetKind.POWER
    assert (guidance.low, guidance.mid, guidance.high) == (220, 231, 242)
    assert guidance.target_text == "Target: 220 - 242 W"
    assert guidance.next_text == "Next: 166 - 180 bpm for 500 m"


def test_ramp_pace_guidance_uses_progress_and_metric_pace() -> None:
    step = WorkoutStep(
        duration=DistanceDuration(meters=1000),
        speed_mps=RampTarget(start=2, end=3),
    )

    guidance = resolve_step_guidance(
        step,
        None,
        progress=0.5,
        personal=_personal(),
        zones=_zones(),
        unit_system=UnitSystem.METRIC,
    )

    assert guidance.domain is TargetDomain.PACE
    assert guidance.pebble_kind is PebbleTargetKind.PACE
    assert (guidance.low, guidance.mid, guidance.high) == (2.5, 2.5, 2.5)
    assert guidance.target_text == "Target: 6:40 - 6:40 /km"
    assert guidance.next_text == "Next: —"


@pytest.mark.parametrize(
    ("unit_system", "expected_next"),
    [
        (UnitSystem.METRIC, "Next: 6:40 - 6:40 /km for 1.61 km"),
        (UnitSystem.IMPERIAL, "Next: 10:44 - 10:44 /mi for 1.00 mi"),
    ],
)
def test_next_step_pace_and_distance_use_the_same_units(unit_system, expected_next) -> None:
    step = WorkoutStep(
        duration=DistanceDuration(meters=1609.344),
        speed_mps=PointTarget(value=2.5),
    )

    guidance = resolve_step_guidance(
        step,
        step,
        personal=_personal(),
        zones=_zones(),
        unit_system=unit_system,
    )

    assert guidance.next_text == expected_next


def test_guidance_without_a_target_has_neutral_values_and_text() -> None:
    guidance = resolve_step_guidance(
        WorkoutStep(duration=DistanceDuration(meters=100)),
        None,
        personal=_personal(),
        zones=_zones(),
    )

    assert guidance.domain is TargetDomain.NONE
    assert guidance.pebble_kind is PebbleTargetKind.NONE
    assert (guidance.low, guidance.mid, guidance.high) == (0, 0, 0)
    assert guidance.target_text == "Target: —"
    assert guidance.next_text == "Next: —"

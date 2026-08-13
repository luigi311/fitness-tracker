"""Validation contracts for small framework-independent core models."""

from dataclasses import fields
from random import Random

import pytest
from fitness_tracker.core.simulator import SensorSimulator, SimulationTarget
from fitness_tracker.core.trainer_mode import TrainerModeConfig
from fitness_tracker.core.units import mps_from_mph
from fitness_tracker.core.zones import ChartTheme, HeartRateZones
from pydantic import ValidationError


@pytest.mark.parametrize(
    ("target_speed_mps", "expected_speed_mps"),
    [
        (1_000.0, mps_from_mph(10.0)),
        (-1_000.0, mps_from_mph(2.0)),
    ],
)
def test_target_driven_simulator_speed_is_clamped_before_distance_accumulates(
    target_speed_mps: float,
    expected_speed_mps: float,
) -> None:
    simulator = SensorSimulator(resting_hr=60, max_hr=190, low_hr=120, rng=Random(0))

    reading = simulator.tick(1.0, SimulationTarget(speed_mps=target_speed_mps))

    assert reading.speed_mps == pytest.approx(expected_speed_mps)
    assert reading.distance_m == pytest.approx(expected_speed_mps)


@pytest.mark.parametrize(
    ("step", "decimals", "message"),
    [
        (0.0, 0, "step must be greater than zero"),
        (-1.0, 0, "step must be greater than zero"),
        (1.0, -1, "decimals must not be negative"),
    ],
)
def test_trainer_mode_config_rejects_invalid_step_and_decimals(
    step: float,
    decimals: int,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        TrainerModeConfig(
            minimum=0,
            maximum=100,
            step=step,
            unit="W",
            decimals=decimals,
        )


@pytest.mark.parametrize(("resting_hr", "max_hr"), [(60, 60), (61, 60)])
def test_heart_rate_zones_require_a_positive_range(resting_hr: float, max_hr: float) -> None:
    with pytest.raises(ValueError, match="max_hr must be greater than resting_hr"):
        HeartRateZones(resting_hr=resting_hr, max_hr=max_hr)


def test_heart_rate_zone_threshold_cache_is_not_compared_or_hashed() -> None:
    threshold_field = next(item for item in fields(HeartRateZones) if item.name == "_thresholds")
    zones = HeartRateZones(resting_hr=60, max_hr=190)

    assert threshold_field.compare is False
    assert threshold_field.hash is False
    assert isinstance(hash(zones), int)


def test_chart_theme_rgb_is_derived_after_model_copy_updates() -> None:
    theme = ChartTheme.for_style(is_dark=True)

    updated = theme.model_copy(update={"zone_colors": ("#000000", "#ffffff")})

    assert updated.zone_rgb == ((0.0, 0.0, 0.0), (1.0, 1.0, 1.0))

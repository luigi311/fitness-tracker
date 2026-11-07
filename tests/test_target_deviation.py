"""Numeric guidance uses band boundaries and the selected display units."""

import pytest
from fitness_tracker.core.guidance import TargetDomain, format_target_deviation
from fitness_tracker.core.units import UnitSystem


@pytest.mark.parametrize(
    ("domain", "current", "low", "high", "expected"),
    [
        (TargetDomain.POWER, 182, 200, 230, "18 W below"),
        (TargetDomain.POWER, 248, 200, 230, "18 W above"),
        (TargetDomain.POWER, 200, 200, 230, "In target"),
        (TargetDomain.POWER, 230, 200, 230, "In target"),
        (TargetDomain.POWER, 199.6, 200, 230, "1 W below"),
        (TargetDomain.PACE, 3.499, 3.5, 4.0, "1 s/km slower"),
        (TargetDomain.HEART_RATE, 148, 156, 164, "8 bpm below"),
        (TargetDomain.HEART_RATE, 172, 156, 164, "8 bpm above"),
        (TargetDomain.PACE, 3.0, 3.5, 4.0, "48 s/km slower"),
        (TargetDomain.PACE, 4.5, 3.5, 4.0, "28 s/km faster"),
        (TargetDomain.PACE, 3.8, 3.5, 4.0, "In target"),
        (TargetDomain.PACE, 0, 3.5, 4.0, "Below target · not moving"),
        (TargetDomain.NONE, 100, 0, 0, "No target"),
    ],
)
def test_target_deviation(domain, current, low, high, expected):
    """Report distance from the nearest target boundary with correct pace direction."""
    assert format_target_deviation(domain, current, low, high, UnitSystem.METRIC) == expected


def test_pace_deviation_uses_mile_distance_for_imperial_units():
    """Express imperial pace deviation in seconds per mile."""
    assert (
        format_target_deviation(TargetDomain.PACE, 3.0, 3.5, 4.0, UnitSystem.IMPERIAL)
        == "77 s/mi slower"
    )

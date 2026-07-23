from fitness_tracker.workouts import apply_target_bias


def test_workout_bias_scales_full_power_target_range() -> None:
    values = apply_target_bias((180.0, 200.0, 220.0), 10, decimal_places=0)

    assert values == (198, 220, 242)


def test_workout_bias_supports_reducing_power() -> None:
    values = apply_target_bias((250.0, 250.0, 250.0), -20, decimal_places=0)

    assert values == (200, 200, 200)


def test_workout_bias_preserves_fractional_speed_to_tenths() -> None:
    values = apply_target_bias((3.3, 3.3, 3.3), 5, decimal_places=1)

    assert values == (3.5, 3.5, 3.5)

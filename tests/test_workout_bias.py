from types import SimpleNamespace

from fitness_tracker.workouts import (
    apply_target_bias,
    has_external_trainer_hr_sensor,
    has_heart_rate_targets,
)


def test_workout_bias_scales_full_power_target_range() -> None:
    values = apply_target_bias((180.0, 200.0, 220.0), 10, decimal_places=0)

    assert values == (198, 220, 242)


def test_workout_bias_supports_reducing_power() -> None:
    values = apply_target_bias((250.0, 250.0, 250.0), -20, decimal_places=0)

    assert values == (200, 200, 200)


def test_workout_bias_preserves_fractional_speed_to_tenths() -> None:
    values = apply_target_bias((3.3, 3.3, 3.3), 5, decimal_places=1)

    assert values == (3.5, 3.5, 3.5)


def test_detects_heart_rate_target_and_external_trainer_hr_sensor() -> None:
    steps = [SimpleNamespace(heart_rate_bpm=SimpleNamespace(value=150))]
    recorder = SimpleNamespace(
        hr_address="AA:BB",
        trainer_address="CC:DD",
        hr_name="Chest strap",
        trainer_name="Smart bike",
    )

    assert has_heart_rate_targets(steps)
    assert has_external_trainer_hr_sensor(recorder)


def test_trainer_itself_is_not_an_external_hr_sensor() -> None:
    recorder = SimpleNamespace(
        hr_address="AA:BB",
        trainer_address="AA:BB",
        hr_name="Smart bike",
        trainer_name="Smart bike",
    )

    assert not has_external_trainer_hr_sensor(recorder)

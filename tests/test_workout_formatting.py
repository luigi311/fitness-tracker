import pytest
from fitness_tracker.core.guidance import format_step_duration
from fitness_tracker.core.units import UnitSystem
from fitness_tracker.workout_execution import WorkoutExecution
from fitness_tracker.workouts import (
    format_step_remaining,
    format_workout_summary,
)
from workout_parser import DistanceDuration, TimeDuration, Workout, WorkoutStep


@pytest.mark.parametrize("unit_system", list(UnitSystem))
def test_format_step_duration_includes_time_units(unit_system) -> None:
    assert (
        format_step_duration(WorkoutStep(duration=TimeDuration(seconds=300)), unit_system)
        == "5 min"
    )
    assert (
        format_step_duration(WorkoutStep(duration=TimeDuration(seconds=90)), unit_system)
        == "1 min 30 s"
    )


@pytest.mark.parametrize(
    ("meters", "unit_system", "expected"),
    [
        (400, UnitSystem.METRIC, "400 m"),
        (1250, UnitSystem.METRIC, "1.25 km"),
        (1609.344, UnitSystem.METRIC, "1.61 km"),
        (400, UnitSystem.IMPERIAL, "0.25 mi"),
        (1250, UnitSystem.IMPERIAL, "0.78 mi"),
        (1609.344, UnitSystem.IMPERIAL, "1.00 mi"),
    ],
)
def test_format_step_duration_uses_selected_distance_units(meters, unit_system, expected) -> None:
    step = WorkoutStep(duration=DistanceDuration(meters=meters))

    assert format_step_duration(step, unit_system) == expected


@pytest.mark.parametrize(
    ("unit_system", "expected_distance"),
    [(UnitSystem.METRIC, "300 m"), (UnitSystem.IMPERIAL, "0.19 mi")],
)
def test_format_step_remaining_uses_the_active_duration_dimension(
    unit_system,
    expected_distance,
) -> None:
    time_execution = WorkoutExecution([WorkoutStep(duration=TimeDuration(seconds=10))])
    distance_execution = WorkoutExecution([WorkoutStep(duration=DistanceDuration(meters=400))])

    assert format_step_remaining(time_execution.update(3, 0), unit_system) == "00:07"
    assert (
        format_step_remaining(distance_execution.update(0, 100), unit_system) == expected_distance
    )
    assert format_step_remaining(distance_execution.update(0, 400), unit_system) == "—"


@pytest.mark.parametrize(
    ("unit_system", "expected_distance", "expected_mixed"),
    [
        (UnitSystem.METRIC, "5.0 km", "20 min + 3.0 km"),
        (UnitSystem.IMPERIAL, "3.11 mi", "20 min + 1.86 mi"),
    ],
)
def test_format_workout_summary_reports_time_distance_and_mixed_totals(
    unit_system,
    expected_distance,
    expected_mixed,
) -> None:
    time_workout = Workout(
        name="Time",
        instructions=(WorkoutStep(duration=TimeDuration(seconds=1200)),),
    )
    distance_workout = Workout(
        name="Distance",
        instructions=(WorkoutStep(duration=DistanceDuration(meters=5000)),),
    )
    mixed_workout = Workout(
        name="Mixed",
        instructions=(
            WorkoutStep(duration=TimeDuration(seconds=1200)),
            WorkoutStep(duration=DistanceDuration(meters=3000)),
        ),
    )

    assert format_workout_summary(time_workout, unit_system) == "20 min"
    assert format_workout_summary(distance_workout, unit_system) == expected_distance
    assert format_workout_summary(mixed_workout, unit_system) == expected_mixed

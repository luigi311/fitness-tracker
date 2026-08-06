from fitness_tracker.workout_execution import WorkoutExecution
from fitness_tracker.workouts import (
    format_step_duration,
    format_step_remaining,
    format_workout_summary,
)
from workout_parser import DistanceDuration, TimeDuration, Workout, WorkoutStep


def test_format_step_duration_includes_time_units() -> None:
    assert format_step_duration(WorkoutStep(duration=TimeDuration(seconds=300))) == "5 min"
    assert format_step_duration(WorkoutStep(duration=TimeDuration(seconds=90))) == "1 min 30 s"


def test_format_step_duration_uses_metric_distance_units() -> None:
    assert format_step_duration(WorkoutStep(duration=DistanceDuration(meters=400))) == "400 m"
    assert format_step_duration(WorkoutStep(duration=DistanceDuration(meters=1250))) == "1.25 km"


def test_format_step_remaining_uses_the_active_duration_dimension() -> None:
    time_execution = WorkoutExecution([WorkoutStep(duration=TimeDuration(seconds=10))])
    distance_execution = WorkoutExecution([WorkoutStep(duration=DistanceDuration(meters=400))])

    assert format_step_remaining(time_execution.update(3, 0)) == "00:07"
    assert format_step_remaining(distance_execution.update(0, 100)) == "300 m"


def test_format_workout_summary_reports_time_distance_and_mixed_totals() -> None:
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

    assert format_workout_summary(time_workout) == "20 min"
    assert format_workout_summary(distance_workout) == "5.0 km"
    assert format_workout_summary(mixed_workout) == "20 min + 3.0 km"

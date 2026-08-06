import pytest
from workout_parser import DistanceDuration, OpenDuration, TimeDuration, WorkoutStep

from fitness_tracker.workout_execution import WorkoutExecution


def _step(duration: TimeDuration | DistanceDuration | OpenDuration) -> WorkoutStep:
    return WorkoutStep(duration=duration)


def test_starts_on_first_time_step() -> None:
    execution = WorkoutExecution([_step(TimeDuration(seconds=10))])

    snapshot = execution.update(0, 0)

    assert snapshot.active_index == 0
    assert snapshot.progress == 0
    assert snapshot.remaining_seconds == 10
    assert snapshot.remaining_meters is None
    assert snapshot.step_changed

    snapshot = execution.update(1, 0)
    assert not snapshot.step_changed


def test_time_overshoot_carries_into_next_time_step() -> None:
    execution = WorkoutExecution(
        [_step(TimeDuration(seconds=10)), _step(TimeDuration(seconds=5))],
    )

    snapshot = execution.update(12, 0)

    assert snapshot.active_index == 1
    assert snapshot.progress == pytest.approx(0.4)
    assert snapshot.remaining_seconds == pytest.approx(3)
    assert snapshot.step_changed


def test_distance_progress_and_overshoot() -> None:
    execution = WorkoutExecution(
        [_step(DistanceDuration(meters=10)), _step(DistanceDuration(meters=5))],
    )

    snapshot = execution.update(0, 12)

    assert snapshot.active_index == 1
    assert snapshot.progress == pytest.approx(0.4)
    assert snapshot.remaining_meters == pytest.approx(3)
    assert snapshot.remaining_seconds is None


def test_time_to_distance_switch_starts_distance_baseline_at_boundary() -> None:
    execution = WorkoutExecution(
        [_step(TimeDuration(seconds=5)), _step(DistanceDuration(meters=100))],
    )

    snapshot = execution.update(5, 20)
    assert snapshot.active_index == 1
    assert snapshot.progress == 0
    assert snapshot.remaining_meters == 100

    snapshot = execution.update(5, 45)
    assert snapshot.progress == pytest.approx(0.25)


def test_distance_to_time_switch_starts_time_baseline_at_boundary() -> None:
    execution = WorkoutExecution(
        [_step(DistanceDuration(meters=100)), _step(TimeDuration(seconds=10))],
    )

    snapshot = execution.update(20, 100)
    assert snapshot.active_index == 1
    assert snapshot.progress == 0
    assert snapshot.remaining_seconds == 10

    snapshot = execution.update(24, 100)
    assert snapshot.progress == pytest.approx(0.4)


def test_mixed_workout_does_not_complete_from_elapsed_time_alone() -> None:
    execution = WorkoutExecution(
        [_step(DistanceDuration(meters=100)), _step(TimeDuration(seconds=10))],
    )

    snapshot = execution.update(100, 50)
    assert not snapshot.completed
    assert snapshot.active_index == 0

    snapshot = execution.update(100, 100)
    assert snapshot.active_index == 1
    assert not snapshot.completed

    snapshot = execution.update(110, 100)
    assert snapshot.completed
    assert snapshot.active_index is None
    assert snapshot.step is None


def test_previous_and_next_enter_steps_at_zero_progress() -> None:
    execution = WorkoutExecution(
        [
            _step(TimeDuration(seconds=10)),
            _step(DistanceDuration(meters=100)),
            _step(TimeDuration(seconds=10)),
        ],
    )
    execution.update(5, 30)

    snapshot = execution.next_step()
    assert snapshot.active_index == 1
    assert snapshot.progress == 0

    snapshot = execution.next_step()
    assert snapshot.active_index == 2
    assert snapshot.progress == 0

    snapshot = execution.previous_step()
    assert snapshot.active_index == 1
    assert snapshot.progress == 0

    snapshot = execution.previous_step()
    assert snapshot.active_index == 0
    assert snapshot.progress == 0

    snapshot = execution.previous_step()
    assert snapshot.active_index == 0
    assert snapshot.progress == 0


def test_next_on_final_step_restarts_final_step() -> None:
    execution = WorkoutExecution([_step(TimeDuration(seconds=10))])
    execution.update(5, 0)

    snapshot = execution.next_step()

    assert snapshot.active_index == 0
    assert snapshot.progress == 0
    assert not snapshot.completed


def test_open_ended_steps_are_rejected_with_specific_message() -> None:
    with pytest.raises(ValueError, match="Open-ended workout steps are not supported yet"):
        WorkoutExecution([_step(OpenDuration())])

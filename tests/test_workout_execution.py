import pytest
from workout_parser import DistanceDuration, OpenDuration, TimeDuration, WorkoutStep

from fitness_tracker.workout_execution import WorkoutDistanceAccumulator, WorkoutExecution


def _step(duration: TimeDuration | DistanceDuration | OpenDuration) -> WorkoutStep:
    return WorkoutStep(duration=duration)


def test_distance_accumulator_establishes_baseline_then_adds_positive_deltas() -> None:
    accumulator = WorkoutDistanceAccumulator()

    accumulator.observe(100, running=True, paused=False)
    accumulator.observe(112.5, running=True, paused=False)

    assert accumulator.available
    assert accumulator.distance_m == pytest.approx(12.5)


def test_distance_accumulator_reset_excludes_movement_before_start() -> None:
    accumulator = WorkoutDistanceAccumulator()
    accumulator.observe(100, running=False, paused=False)
    accumulator.observe(105, running=False, paused=False)

    accumulator.reset()
    accumulator.observe(110, running=True, paused=False)
    accumulator.observe(113, running=True, paused=False)

    assert accumulator.distance_m == pytest.approx(3)


def test_distance_accumulator_ignores_paused_movement() -> None:
    accumulator = WorkoutDistanceAccumulator()
    accumulator.observe(100, running=True, paused=False)
    accumulator.observe(125, running=True, paused=True)
    accumulator.observe(125, running=True, paused=False)
    accumulator.observe(130, running=True, paused=False)

    assert accumulator.distance_m == pytest.approx(5)


def test_distance_accumulator_rebaselines_decreasing_readings() -> None:
    accumulator = WorkoutDistanceAccumulator()
    accumulator.observe(100, running=True, paused=False)
    accumulator.observe(110, running=True, paused=False)
    accumulator.observe(40, running=True, paused=False)
    accumulator.observe(45, running=True, paused=False)

    assert accumulator.distance_m == pytest.approx(15)


def test_distance_accumulator_rebaselines_after_reconnect() -> None:
    accumulator = WorkoutDistanceAccumulator()
    accumulator.observe(100, running=True, paused=False)
    accumulator.observe(120, running=True, paused=False)
    accumulator.reset_raw_baseline()
    accumulator.observe(150, running=True, paused=False)

    assert accumulator.distance_m == pytest.approx(20)


def test_distance_accumulator_ignores_missing_and_invalid_readings() -> None:
    accumulator = WorkoutDistanceAccumulator()
    for reading in (None, float("nan"), float("inf"), -1):
        accumulator.observe(reading, running=True, paused=False)

    accumulator.observe(10, running=True, paused=False)

    assert not accumulator.distance_m
    assert accumulator.available


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

"""Mutable state for one active workout session."""

from collections.abc import Sequence
from typing import TypeVar

from fitness_tracker.core.trainer_mode import TrainerMode

TWorkout = TypeVar("TWorkout")
TStep = TypeVar("TStep")
TExecution = TypeVar("TExecution")
TSnapshot = TypeVar("TSnapshot")
TAccumulator = TypeVar("TAccumulator")


class WorkoutSession[TWorkout, TStep, TExecution, TSnapshot, TAccumulator]:
    """Own the mutable state that exists only while a workout is active."""

    def __init__(
        self,
        *,
        workout: TWorkout,
        steps: Sequence[TStep],
        execution: TExecution,
        distance_accumulator: TAccumulator,
    ) -> None:
        self.workout = workout
        self.steps = tuple(steps)
        self.execution = execution
        self.distance_accumulator = distance_accumulator
        self.snapshot: TSnapshot | None = None
        self.pending_step_change = False
        self.distance_source_connected: bool | None = None
        self.distance_waiting_notified = False
        self.manual_offset_s = 0.0
        self.pause_started_monotonic: float | None = None
        self.trainer_control_mode = TrainerMode.BIAS
        self.manual_speed_kmh: float | None = None
        self.bias_percent = 0

    def defer_step_change(self) -> None:
        """Keep a step-change edge until guidance can render it."""
        self.pending_step_change = True

    def consume_pending_step_change(self) -> bool:
        """Return and clear a step-change edge deferred by a sample update."""
        pending = self.pending_step_change
        self.pending_step_change = False
        return pending

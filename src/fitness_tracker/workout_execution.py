"""Duration-aware workout progression without UI dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import TYPE_CHECKING

from workout_parser import DistanceDuration, OpenDuration, TimeDuration, WorkoutStep

if TYPE_CHECKING:
    from collections.abc import Sequence


SupportedDuration = TimeDuration | DistanceDuration


class EmptyWorkoutError(ValueError):
    """Raised when an execution is created without any steps."""

    def __init__(self) -> None:
        super().__init__("At least one workout step is required")


class OpenEndedWorkoutError(ValueError):
    """Raised when an execution contains an open-ended step."""

    def __init__(self) -> None:
        super().__init__("Open-ended workout steps are not supported yet")


class UnsupportedWorkoutDurationError(TypeError):
    """Raised when a step uses an unknown duration type."""

    def __init__(self, duration_type: str) -> None:
        super().__init__(f"Unsupported workout step duration: {duration_type}")


@dataclass(slots=True)
class WorkoutDistanceAccumulator:
    """Accumulate valid sensor distance only while a workout is moving."""

    _last_raw_distance_m: float | None = None
    distance_m: float = 0.0
    available: bool = False

    def reset(self) -> None:
        """Clear the effective distance and the raw sensor baseline."""
        self._last_raw_distance_m = None
        self.distance_m = 0.0
        self.available = False

    def reset_raw_baseline(self) -> None:
        """Forget a possibly stale raw value without losing effective distance."""
        self._last_raw_distance_m = None

    def observe(
        self,
        raw_distance_m: float | None,
        *,
        running: bool,
        paused: bool,
    ) -> None:
        """Process one raw distance reading from the active workout sensor."""
        if raw_distance_m is None:
            return
        try:
            raw_distance = float(raw_distance_m)
        except (TypeError, ValueError):
            return
        if not isfinite(raw_distance) or raw_distance < 0:
            return

        self.available = True
        previous = self._last_raw_distance_m
        self._last_raw_distance_m = raw_distance
        if previous is None or raw_distance < previous:
            return
        if running and not paused:
            self.distance_m += raw_distance - previous


@dataclass(frozen=True, slots=True)
class WorkoutExecutionSnapshot:
    """The active state of a workout after an observation or navigation action."""

    active_index: int | None
    step: WorkoutStep | None
    progress: float
    remaining_seconds: float | None
    remaining_meters: float | None
    step_changed: bool
    completed: bool


class WorkoutExecution:
    """Progress through expanded workout steps using time and/or distance."""

    def __init__(
        self,
        steps: Sequence[WorkoutStep],
        *,
        elapsed_s: float = 0.0,
        distance_m: float = 0.0,
    ) -> None:
        self.steps = tuple(steps)
        if not self.steps:
            raise EmptyWorkoutError
        for step in self.steps:
            self._supported_duration(step)

        self._elapsed_s = self._validate_measurement(elapsed_s, "elapsed_s")
        self._distance_m = self._validate_measurement(distance_m, "distance_m")
        self.active_index: int | None = 0
        self.step_started_elapsed_s = self._elapsed_s
        self.step_started_distance_m = self._distance_m
        self.completed = False
        self._initial_step_pending = True

    def update(self, elapsed_s: float, distance_m: float) -> WorkoutExecutionSnapshot:
        """Apply the latest monotonic workout-time and workout-distance values."""
        self._observe(elapsed_s, distance_m)
        step_changed = self._initial_step_pending
        self._initial_step_pending = False

        while not self.completed:
            if self.active_index is None:
                raise RuntimeError("Incomplete workout has no active step index")
            duration = self._supported_duration(self.steps[self.active_index])
            consumed = self._consumed(duration)
            limit = self._duration_value(duration)
            if consumed < limit:
                break

            if self.active_index == len(self.steps) - 1:
                self.completed = True
                self.active_index = None
                step_changed = True
                break

            previous_duration = duration
            self.active_index += 1
            next_duration = self._supported_duration(self.steps[self.active_index])
            same_dimension = (
                isinstance(previous_duration, TimeDuration)
                and isinstance(next_duration, TimeDuration)
            ) or (
                isinstance(previous_duration, DistanceDuration)
                and isinstance(next_duration, DistanceDuration)
            )
            if same_dimension and isinstance(previous_duration, TimeDuration):
                self.step_started_elapsed_s += float(previous_duration.seconds)
            elif same_dimension and isinstance(previous_duration, DistanceDuration):
                self.step_started_distance_m += float(previous_duration.meters)
            else:
                self.step_started_elapsed_s = self._elapsed_s
                self.step_started_distance_m = self._distance_m
            step_changed = True

        return self._snapshot(step_changed=step_changed)

    def previous_step(
        self,
        *,
        elapsed_s: float | None = None,
        distance_m: float | None = None,
    ) -> WorkoutExecutionSnapshot:
        """Enter the preceding step at zero progress, or restart the first step."""
        self._observe_optional(elapsed_s, distance_m)
        if self.completed:
            target_index = len(self.steps) - 1
        else:
            if self.active_index is None:
                raise RuntimeError("Cannot move to previous step without an active step index")
            target_index = max(0, self.active_index - 1)
        return self._enter_step(target_index)

    def next_step(
        self,
        *,
        elapsed_s: float | None = None,
        distance_m: float | None = None,
    ) -> WorkoutExecutionSnapshot:
        """Enter the next step, or restart the final step instead of completing."""
        self._observe_optional(elapsed_s, distance_m)
        if self.completed:
            return self._snapshot(step_changed=False)

        if self.active_index is None:
            raise RuntimeError("Cannot move to next step without an active step index")
        target_index = min(self.active_index + 1, len(self.steps) - 1)
        return self._enter_step(target_index)

    def snapshot(self) -> WorkoutExecutionSnapshot:
        """Return the current state without consuming a pending change marker."""
        return self._snapshot(step_changed=False)

    def _observe(self, elapsed_s: float, distance_m: float) -> None:
        self._elapsed_s = max(self._elapsed_s, self._validate_measurement(elapsed_s, "elapsed_s"))
        self._distance_m = max(
            self._distance_m,
            self._validate_measurement(distance_m, "distance_m"),
        )

    def _observe_optional(self, elapsed_s: float | None, distance_m: float | None) -> None:
        if elapsed_s is not None:
            self._elapsed_s = max(
                self._elapsed_s,
                self._validate_measurement(elapsed_s, "elapsed_s"),
            )
        if distance_m is not None:
            self._distance_m = max(
                self._distance_m,
                self._validate_measurement(distance_m, "distance_m"),
            )

    def _enter_step(self, index: int) -> WorkoutExecutionSnapshot:
        self.active_index = index
        self.completed = False
        self.step_started_elapsed_s = self._elapsed_s
        self.step_started_distance_m = self._distance_m
        self._initial_step_pending = False
        return self._snapshot(step_changed=True)

    def _consumed(self, duration: SupportedDuration) -> float:
        if isinstance(duration, TimeDuration):
            return self._elapsed_s - self.step_started_elapsed_s
        return self._distance_m - self.step_started_distance_m

    @staticmethod
    def _duration_value(duration: SupportedDuration) -> float:
        if isinstance(duration, TimeDuration):
            return float(duration.seconds)
        return float(duration.meters)

    def _snapshot(self, *, step_changed: bool) -> WorkoutExecutionSnapshot:
        if self.completed:
            return WorkoutExecutionSnapshot(
                active_index=None,
                step=None,
                progress=1.0,
                remaining_seconds=None,
                remaining_meters=None,
                step_changed=step_changed,
                completed=True,
            )

        if self.active_index is None:
            raise RuntimeError("Cannot snapshot incomplete workout without an active step index")
        step = self.steps[self.active_index]
        duration = self._supported_duration(step)
        consumed = max(0.0, self._consumed(duration))
        limit = self._duration_value(duration)
        progress = min(1.0, consumed / limit) if limit > 0 else 0.0
        remaining = max(0.0, limit - consumed)
        if isinstance(duration, TimeDuration):
            remaining_seconds = remaining
            remaining_meters = None
        else:
            remaining_seconds = None
            remaining_meters = remaining
        return WorkoutExecutionSnapshot(
            active_index=self.active_index,
            step=step,
            progress=progress,
            remaining_seconds=remaining_seconds,
            remaining_meters=remaining_meters,
            step_changed=step_changed,
            completed=False,
        )

    @staticmethod
    def _supported_duration(step: WorkoutStep) -> SupportedDuration:
        duration = step.duration
        if isinstance(duration, (TimeDuration, DistanceDuration)):
            return duration
        if isinstance(duration, OpenDuration):
            raise OpenEndedWorkoutError
        raise UnsupportedWorkoutDurationError(type(duration).__name__)

    @staticmethod
    def _validate_measurement(value: float, name: str) -> float:
        message = f"{name} must be finite and non-negative"
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(message) from exc
        if not isfinite(numeric) or numeric < 0:
            raise ValueError(message)
        return numeric

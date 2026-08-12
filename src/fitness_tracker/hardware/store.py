"""Persistence boundary used by hardware recording components."""

from typing import Protocol

from bleaksport.models import CyclingSample, RunningSample, TrainerSample

from fitness_tracker.core.sports import SportTypesEnum


class RecordingStore(Protocol):
    """Operations a recorder needs without knowing the database implementation."""

    def start_activity(self, sport_type: SportTypesEnum) -> int:
        """Create and return the identifier for a new activity."""

    def finalize_activity(self, activity_id: int) -> None:
        """Flush measurements, close the activity, and compute its statistics."""

    def insert_heart_rate(
        self,
        activity_id: int,
        timestamp_ms: int,
        bpm: int,
        rr: float | None,
    ) -> None:
        """Persist one normalized heart-rate measurement."""

    def insert_running_metrics(
        self,
        activity_id: int,
        sample: RunningSample | TrainerSample,
        incline_percent: float | None,
    ) -> None:
        """Persist one normalized running measurement."""

    def insert_cycling_metrics(
        self,
        activity_id: int,
        sample: CyclingSample | TrainerSample,
        incline_percent: float | None,
    ) -> None:
        """Persist one normalized cycling measurement."""

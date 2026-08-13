"""Intervals.icu service facade for UI callers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ValidationError

from fitness_tracker.integrations.errors import (
    IntegrationConfigurationError,
    IntegrationError,
)
from fitness_tracker.integrations.intervals_icu import (
    IntervalsICUClient,
    IntervalsICUCredentials,
)
from fitness_tracker.upload_providers.intervals_icu import IntervalsICUUploader
from fitness_tracker.workout_providers.intervals_icu import IntervalsICUProvider

if TYPE_CHECKING:
    from datetime import date
    from pathlib import Path

    from fitness_tracker.data.repositories import ActivityRepository
    from fitness_tracker.workout_providers.utils import WorkoutRefreshResult

__all__ = [
    "IntegrationError",
    "refresh_intervals_workouts",
    "upload_intervals_activities",
]
_INTEGRATION_NAME = "intervals.icu"


def _create_client(athlete_id: str | None, api_key: str | None) -> IntervalsICUClient:
    try:
        credentials = IntervalsICUCredentials(
            athlete_id=athlete_id or "",
            api_key=api_key or "",
        )
    except ValidationError as exc:
        message = "required configuration values must not be empty"
        raise IntegrationConfigurationError(_INTEGRATION_NAME, message) from exc
    return IntervalsICUClient(credentials)


def refresh_intervals_workouts(
    *,
    athlete_id: str | None,
    api_key: str | None,
    start: date,
    end: date,
    running_dir: Path,
    cycling_dir: Path,
) -> WorkoutRefreshResult:
    """Fetch one event snapshot and refresh both sport directories atomically."""
    provider = IntervalsICUProvider(client=_create_client(athlete_id, api_key), ext="fit")
    return provider.refresh_between(
        start=start,
        end=end,
        running_dir=running_dir,
        cycling_dir=cycling_dir,
    )


def upload_intervals_activities(
    *,
    athlete_id: str | None,
    api_key: str | None,
    repository: ActivityRepository,
) -> list[tuple[int, bool, str | None]]:
    """Upload activities through the validated Intervals.icu client."""
    uploader = IntervalsICUUploader(client=_create_client(athlete_id, api_key))
    return uploader.upload_not_uploaded(repository)

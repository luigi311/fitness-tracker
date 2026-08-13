from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class DownloadedWorkout(BaseModel):
    """Represents a file we just wrote to disk."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: Path
    start_date: date  # local date the workout is planned for
    title: str  # provider workout title (best effort)


class WorkoutRefreshResult(BaseModel):
    """Outcome of an atomic workout refresh."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    written: tuple[DownloadedWorkout, ...] = ()
    skipped: int = 0
    invalid: int = 0

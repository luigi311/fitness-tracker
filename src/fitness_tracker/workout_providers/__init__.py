from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol, Iterable, Literal

Sport = Literal["running", "cycling"]

@dataclass(frozen=True)
class DownloadedWorkout:
    """Represents a file we just wrote to disk."""
    path: Path
    start_date: date        # local date the workout is planned for
    title: str              # provider workout title (best effort)

class WorkoutProvider(Protocol):
    name: str
    def fetch_between(
        self,
        sport: Sport,
        start: date,
        end: date,
        out_dir: Path,
    ) -> Iterable[DownloadedWorkout]:
        """Fetch planned workouts in [start, end] (inclusive) for a given sport and write files into out_dir."""
        ...

def week_window_from(today: date) -> tuple[date, date]:
    # today..sunday (or today..today if today is Sunday)
    end = today + timedelta(days=(6 - today.weekday())) if today.weekday() <= 6 else today
    return today, end

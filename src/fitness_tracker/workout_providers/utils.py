
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

Sport = Literal["running", "cycling"]

@dataclass(frozen=True)
class DownloadedWorkout:
    """Represents a file we just wrote to disk."""
    path: Path
    start_date: date        # local date the workout is planned for
    title: str              # provider workout title (best effort)

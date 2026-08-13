from datetime import date, datetime
from pathlib import Path

from workout_parser import (
    DistanceDuration,
    OpenDuration,
    TimeDuration,
    Workout,
)

from fitness_tracker.core.guidance import apply_target_bias, format_step_duration
from fitness_tracker.core.units import (
    DurationStyle,
    UnitSystem,
    format_distance,
    format_duration,
    format_human_duration,
)
from fitness_tracker.workout_execution import WorkoutExecutionSnapshot

# -----------------------
# Discovery
# -----------------------

AUTO_SUBDIRS = ("intervals_icu",)
_WORKOUT_LOOKAHEAD_DAYS = 6


def format_step_remaining(snapshot: WorkoutExecutionSnapshot) -> str:
    """Return the active step's remaining amount with its unit."""
    if snapshot.remaining_seconds is not None:
        return format_duration(snapshot.remaining_seconds, DurationStyle.COUNTDOWN)
    if snapshot.remaining_meters is not None:
        return format_distance(
            snapshot.remaining_meters,
            UnitSystem.METRIC,
            include_whole_km_decimal=False,
        )
    return "—"


def format_workout_summary(workout: Workout) -> str:
    """Return the known time and distance totals for a workout."""
    total_seconds = 0.0
    total_meters = 0.0
    has_open_duration = False
    for step in workout.expanded_steps():
        if isinstance(step.duration, TimeDuration):
            total_seconds += float(step.duration.seconds)
        elif isinstance(step.duration, DistanceDuration):
            total_meters += float(step.duration.meters)
        elif isinstance(step.duration, OpenDuration):
            has_open_duration = True

    parts: list[str] = []
    if total_seconds:
        parts.append(format_human_duration(total_seconds))
    if total_meters:
        parts.append(
            format_distance(
                total_meters,
                UnitSystem.METRIC,
                include_whole_km_decimal=True,
            ),
        )
    if has_open_duration:
        parts.append("Open")
    return " + ".join(parts)


def _date_from_filename(p: Path) -> date | None:
    # YYYY-MM-DD Title.ext
    try:
        return date.fromisoformat(p.stem.split(" ", 1)[0])
    except Exception:
        return None


def discover_workouts(running_dir: Path) -> list[Path]:
    """
    Return workout files in the order.

      1) Today's dated auto files
      2) Other dated auto files later this week (ascending date)
      3) Manual files in the root 'running' directory
    """
    today = datetime.now().astimezone().date()

    # Collect auto files from provider subfolders
    auto_files: list[Path] = []
    for sub in AUTO_SUBDIRS:
        d = running_dir / sub
        if d.is_dir():
            auto_files.extend([p for p in d.glob("*.*") if p.is_file()])

    # Partition autos by date
    todays: list[tuple[date, Path]] = []
    weeks: list[tuple[date, Path]] = []
    for p in auto_files:
        d = _date_from_filename(p)
        if not d:
            continue
        if d == today:
            todays.append((d, p))
        elif 0 <= (d - today).days <= _WORKOUT_LOOKAHEAD_DAYS:
            weeks.append((d, p))

    todays.sort(key=lambda t: t[0])  # single day but deterministic
    weeks.sort(key=lambda t: t[0])  # ascending date

    # Manual files live in running_dir root (ignore provider subdirs)
    manual = sorted(
        [p for p in running_dir.glob("*.*") if p.is_file()],
        key=lambda p: p.stem.lower(),
    )

    # Stitch in order
    return [p for _, p in todays] + [p for _, p in weeks] + manual

from datetime import date
from pathlib import Path

from workout_parser import (
    DistanceDuration,
    OpenDuration,
    TimeDuration,
    Workout,
    WorkoutStep,
)

from fitness_tracker.workout_execution import WorkoutExecutionSnapshot

# -----------------------
# Discovery
# -----------------------

AUTO_SUBDIRS = ("intervals_icu",)
_DISTANCE_KM_THRESHOLD_M = 1000
_SECONDS_INTEGER_TOLERANCE = 1e-6


def _format_number(value: float) -> str:
    """Format a measurement without unnecessary trailing zeroes."""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _format_time_duration(seconds: float) -> str:
    """Format a step duration in a compact, human-readable form."""
    seconds = max(0.0, float(seconds))
    rounded_seconds = round(seconds)
    if abs(seconds - rounded_seconds) < _SECONDS_INTEGER_TOLERANCE:
        total_seconds = int(rounded_seconds)
        minutes, remainder = divmod(total_seconds, 60)
        if minutes:
            if remainder:
                return f"{minutes} min {remainder} s"
            return f"{minutes} min"
        return f"{remainder} s"
    return f"{seconds:.1f} s"


def _format_time_remaining(seconds: float) -> str:
    """Format time remaining using the active workout clock convention."""
    total_seconds = max(0, int(float(seconds)))
    minutes, remainder = divmod(total_seconds, 60)
    return f"{minutes:02d}:{remainder:02d}"


def _format_distance_duration(meters: float) -> str:
    """Format a distance in metres below one kilometre and kilometres above it."""
    meters = max(0.0, float(meters))
    if meters < _DISTANCE_KM_THRESHOLD_M:
        return f"{_format_number(meters)} m"
    return f"{_format_number(meters / _DISTANCE_KM_THRESHOLD_M)} km"


def _format_summary_distance(meters: float) -> str:
    """Format a workout total distance with one decimal for whole kilometres."""
    meters = max(0.0, float(meters))
    if meters < _DISTANCE_KM_THRESHOLD_M:
        return f"{_format_number(meters)} m"
    kilometers = _format_number(meters / _DISTANCE_KM_THRESHOLD_M)
    if "." not in kilometers:
        kilometers += ".0"
    return f"{kilometers} km"


def format_step_duration(step: WorkoutStep) -> str:
    """Return the configured duration of a workout step with its unit."""
    duration = step.duration
    if isinstance(duration, TimeDuration):
        return _format_time_duration(duration.seconds)
    if isinstance(duration, DistanceDuration):
        return _format_distance_duration(duration.meters)
    if isinstance(duration, OpenDuration):
        return "Open"
    return str(duration)


def format_step_remaining(snapshot: WorkoutExecutionSnapshot) -> str:
    """Return the active step's remaining amount with its unit."""
    if snapshot.remaining_seconds is not None:
        return _format_time_remaining(snapshot.remaining_seconds)
    if snapshot.remaining_meters is not None:
        return _format_distance_duration(snapshot.remaining_meters)
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
        parts.append(_format_time_duration(total_seconds))
    if total_meters:
        parts.append(_format_summary_distance(total_meters))
    if has_open_duration:
        parts.append("Open")
    return " + ".join(parts)


def has_heart_rate_targets(steps) -> bool:
    """Return whether any workout step contains a supported heart-rate target."""
    fields = (
        "heart_rate_bpm",
        "heart_rate_percent_max",
        "heart_rate_percent_lthr",
        "heart_rate_zone",
    )
    return any(any(getattr(step, field, None) is not None for field in fields) for step in steps)


def apply_target_bias(
    values: tuple[float, float, float] | None,
    percent: int,
    decimal_places: int | None = None,
) -> tuple[float, float, float] | None:
    """Scale a resolved workout target by the trainer bias percentage."""
    if values is None:
        return None
    factor = 1.0 + percent / 100.0
    low, current, high = values
    adjusted = low * factor, current * factor, high * factor
    if decimal_places is None:
        return adjusted
    return (
        round(adjusted[0], decimal_places),
        round(adjusted[1], decimal_places),
        round(adjusted[2], decimal_places),
    )


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
    today = date.today()

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
        elif 0 <= (d - today).days <= 6:
            weeks.append((d, p))

    todays.sort(key=lambda t: t[0])  # single day but deterministic
    weeks.sort(key=lambda t: t[0])  # ascending date

    # Manual files live in running_dir root (ignore provider subdirs)
    manual = sorted(
        [p for p in running_dir.glob("*.*") if p.is_file()],
        key=lambda p: p.stem.lower(),
    )

    # Stitch in order
    ordered = [p for _, p in todays] + [p for _, p in weeks] + manual
    return ordered

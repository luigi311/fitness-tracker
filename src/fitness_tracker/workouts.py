from datetime import date
from pathlib import Path

# -----------------------
# Discovery
# -----------------------

SUPPORTED_EXTS = (".fit", ".json")
AUTO_SUBDIRS = ("intervals_icu",)


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

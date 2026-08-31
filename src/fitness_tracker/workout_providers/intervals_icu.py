from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from loguru import logger

from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.integrations.errors import IntegrationError
from fitness_tracker.workout_providers.utils import DownloadedWorkout, WorkoutRefreshResult

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import date

    from fitness_tracker.integrations.intervals_icu import IcuWorkoutEvent, IntervalsICUClient

_MANAGED_SUFFIXES = frozenset({".fit", ".zwo", ".erg", ".mrc", ".json"})


@dataclass(frozen=True)
class _PreparedRefresh:
    out_dir: Path
    stage_dir: Path
    result: WorkoutRefreshResult


def _is_managed_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in _MANAGED_SUFFIXES


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


@dataclass
class IntervalsICUProvider:
    """Fetch and atomically replace Intervals.icu workout JSON files."""

    client: IntervalsICUClient
    ext: str = "json"

    def refresh_between(
        self,
        *,
        start: date,
        end: date,
        running_dir: Path,
        cycling_dir: Path,
    ) -> WorkoutRefreshResult:
        """Fetch once and prepare both sport directories from one snapshot."""
        prepared: list[_PreparedRefresh] = []
        results: list[WorkoutRefreshResult] = []
        phase = "fetch events"
        logger.debug(
            "Intervals.icu workout refresh starting: start={}, end={}, ext={}, "
            "running_dir={}, cycling_dir={}",
            start,
            end,
            self.ext,
            running_dir,
            cycling_dir,
        )
        try:
            events = self.client.fetch_events(start=start, end=end, ext=self.ext)
            skipped = sum(
                not self._matches_sport(event, SportTypesEnum.running)
                and not self._matches_sport(event, SportTypesEnum.biking)
                for event in events
            )
            logger.debug(
                "Intervals.icu snapshot ready for refresh: events={}, unsupported={}",
                len(events),
                skipped,
            )
            for sport, out_dir in (
                (SportTypesEnum.running, running_dir),
                (SportTypesEnum.biking, cycling_dir),
            ):
                phase = f"prepare {sport.name} workouts"
                out_dir.mkdir(parents=True, exist_ok=True)
                current, result = self._prepare_refresh(sport, start, events, out_dir)
                results.append(result)
                if current is not None:
                    prepared.append(current)
            if prepared:
                phase = "commit staged workouts"
                self._commit_staged(prepared)
        except Exception as error:
            for current in prepared:
                _remove_path(current.stage_dir)
            if isinstance(error, IntegrationError):
                logger.error(
                    "Intervals.icu workout refresh failed: phase={}, error_type={}, status={}, "
                    "detail={}",
                    phase,
                    type(error).__name__,
                    getattr(error, "status_code", None),
                    error.debug_detail,
                )
            else:
                logger.opt(exception=error).error(
                    "Intervals.icu workout refresh failed: phase={}, prepared_directories={}",
                    phase,
                    len(prepared),
                )
            raise

        result = WorkoutRefreshResult(
            written=tuple(workout for result in results for workout in result.written),
            skipped=skipped,
            invalid=sum(result.invalid for result in results),
        )
        logger.debug(
            "Intervals.icu workout refresh completed: written={}, skipped={}, invalid={}",
            len(result.written),
            result.skipped,
            result.invalid,
        )
        return result

    def _prepare_refresh(
        self,
        sport: SportTypesEnum,
        start: date,
        events: Sequence[IcuWorkoutEvent],
        out_dir: Path,
    ) -> tuple[_PreparedRefresh | None, WorkoutRefreshResult]:
        if out_dir.exists() and not out_dir.is_dir():
            raise NotADirectoryError(out_dir)

        stage_dir = Path(
            tempfile.mkdtemp(prefix=f".{out_dir.name}.refresh-", dir=out_dir.parent),
        )
        logger.trace(
            "Preparing Intervals.icu workout directory: sport={}, destination={}, stage={}",
            sport.name,
            out_dir,
            stage_dir,
        )
        written: list[DownloadedWorkout] = []
        invalid = 0
        used_names: set[str] = set()
        try:
            self._copy_unmanaged_files(out_dir, stage_dir)
            for index, event in enumerate(events):
                if not self._matches_sport(event, sport):
                    continue

                if not event.workout_file_base64 or not event.workout_filename:
                    invalid += 1
                    logger.debug(
                        "Skipping incomplete Intervals.icu workout: sport={}, event_index={}, "
                        "has_filename={}, has_workout_data={}",
                        sport.name,
                        index,
                        bool(event.workout_filename),
                        bool(event.workout_file_base64),
                    )
                    continue

                workout_date = event.planned_date or start
                safe_title = self._safe_title(event)
                output_name = self._unique_output_name(
                    workout_date=workout_date,
                    safe_title=safe_title,
                    used_names=used_names,
                )
                stage_path = stage_dir / output_name
                with stage_path.open("w", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(event.model_dump(mode="json"), ensure_ascii=False),
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                written.append(
                    DownloadedWorkout(
                        path=out_dir / output_name,
                        start_date=workout_date,
                        title=safe_title,
                    ),
                )
                logger.trace(
                    "Staged Intervals.icu workout: sport={}, event_index={}, "
                    "planned_date={}, output={}",
                    sport.name,
                    index,
                    workout_date,
                    out_dir / output_name,
                )

            result = WorkoutRefreshResult(
                written=tuple(written),
                invalid=invalid,
            )
            if not written:
                logger.debug(
                    "No valid Intervals.icu workouts for sport={}; preserving existing files "
                    "(invalid={})",
                    sport.name,
                    invalid,
                )
                _remove_path(stage_dir)
                return None, result
            logger.debug(
                "Prepared Intervals.icu workouts: sport={}, written={}, invalid={}",
                sport.name,
                len(written),
                invalid,
            )
            return _PreparedRefresh(out_dir, stage_dir, result), result
        except Exception:
            _remove_path(stage_dir)
            raise

    @staticmethod
    def _copy_unmanaged_files(out_dir: Path, stage_dir: Path) -> None:
        if not out_dir.exists():
            return
        for child in out_dir.iterdir():
            if _is_managed_file(child):
                continue
            target = stage_dir / child.name
            if child.is_dir():
                shutil.copytree(child, target)
            elif child.is_file():
                shutil.copy2(child, target)

    @staticmethod
    def _matches_sport(event: IcuWorkoutEvent, sport: SportTypesEnum) -> bool:
        event_type = event.type.strip()
        return (sport == SportTypesEnum.running and event_type == "Run") or (
            sport == SportTypesEnum.biking and event_type == "Ride"
        )

    @staticmethod
    def _safe_title(event: IcuWorkoutEvent) -> str:
        filename = event.workout_filename or "workout"
        title = (event.name or event.title or Path(filename).stem).strip()
        safe_title = "".join(
            character if character.isalnum() or character in " -_." else "_" for character in title
        ).strip(" .")
        return safe_title or "Workout"

    @staticmethod
    def _unique_output_name(
        *,
        workout_date: date,
        safe_title: str,
        used_names: set[str],
    ) -> str:
        stem = f"{workout_date.isoformat()} {safe_title}"
        output_name = f"{stem}.json"
        suffix = 2
        while output_name in used_names:
            output_name = f"{stem} ({suffix}).json"
            suffix += 1
        used_names.add(output_name)
        return output_name

    @staticmethod
    def _commit_staged(prepared: Iterable[_PreparedRefresh]) -> None:
        staged = list(prepared)
        logger.debug("Committing Intervals.icu workout directories: count={}", len(staged))
        backups: list[tuple[Path, Path | None]] = []
        installed: list[_PreparedRefresh] = []
        try:
            for current in staged:
                # Keep the ordered loop so a failed rename can be rolled back.
                backups.append(  # noqa: PERF401
                    (current.out_dir, IntervalsICUProvider._backup_directory(current.out_dir)),
                )

            for current in staged:
                current.stage_dir.replace(current.out_dir)
                installed.append(current)
                logger.trace("Installed Intervals.icu workout directory: {}", current.out_dir)
        except Exception:
            logger.warning(
                "Intervals.icu workout commit failed; rolling back: backed_up={}, installed={}",
                len(backups),
                len(installed),
            )
            IntervalsICUProvider._rollback_staged(backups, installed)
            raise
        else:
            IntervalsICUProvider._remove_backups(backups)
            logger.debug("Intervals.icu workout directory commit completed")
        finally:
            for current in staged:
                if current.stage_dir.exists():
                    _remove_path(current.stage_dir)

    @staticmethod
    def _backup_directory(out_dir: Path) -> Path | None:
        if not out_dir.exists():
            return None
        backup = out_dir.with_name(f".{out_dir.name}.backup-{uuid4().hex}")
        out_dir.replace(backup)
        return backup

    @staticmethod
    def _rollback_staged(
        backups: list[tuple[Path, Path | None]],
        installed: list[_PreparedRefresh],
    ) -> None:
        for current in reversed(installed):
            _remove_path(current.out_dir)
        for out_dir, backup in reversed(backups):
            if backup is None or not backup.exists():
                continue
            if out_dir.exists():
                _remove_path(out_dir)
            backup.replace(out_dir)

    @staticmethod
    def _remove_backups(backups: list[tuple[Path, Path | None]]) -> None:
        for _out_dir, backup in backups:
            if backup is not None and backup.exists():
                _remove_path(backup)

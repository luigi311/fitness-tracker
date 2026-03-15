from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Session

from fitness_tracker.database import (
    Activity,
    ActivitySport,
    ActivityStats,
    Base,
    CyclingMetrics,
    HeartRate,
    RunningMetrics,
    SportTypesEnum,
)
from fitness_tracker.exporters import infer_sport

if TYPE_CHECKING:
    from fitness_tracker.database import DatabaseManager


def _safe_avg(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    return sum(clean) / len(clean) if clean else None


def _calc_elevation(samples: list) -> tuple[float, float]:
    """Return (total_ascent_m, total_descent_m) from a list of metric rows."""
    ascent = 0.0
    descent = 0.0
    prev_alt: float | None = None
    for s in samples:
        alt = getattr(s, "altitude_m", None)
        if alt is None:
            continue
        alt = float(alt)
        if prev_alt is not None:
            diff = alt - prev_alt
            if diff > 0:
                ascent += diff
            else:
                descent += abs(diff)
        prev_alt = alt
    return ascent, descent


def _last_distance(samples: list) -> float | None:
    """Return the last non-None total_distance_m from a sample list."""
    for s in reversed(samples):
        d = getattr(s, "total_distance_m", None)
        if d is not None:
            return float(d)
    return None


# ---------------------------------------------------------------------------
# Calculator
# ---------------------------------------------------------------------------


class StatsCalculator:
    """Compute and persist ActivityStats rows.

    Usage::

        calc = StatsCalculator(db_manager)

        # After a workout finishes:
        calc.compute_for_activity(activity_id)

        # One-time back-fill / maintenance:
        calc.compute_all(force=False)   # skip already-computed
        calc.compute_all(force=True)    # recompute everything
    """

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_for_activity(self, activity_id: int) -> ActivityStats | None:
        """Compute (or recompute) stats for one activity.  Returns the row."""
        with self.db.Session() as session:
            act = session.get(Activity, activity_id)
            if act is None:
                logger.warning(f"compute_for_activity: activity {activity_id} not found")
                return None
            row = self._build_stats_row(session, act)
            if row is None:
                return None
            self._upsert(session, row)
            session.commit()
            logger.debug(f"Stats computed for activity {activity_id}")
            return row

    def compute_all(self, *, force: bool = False) -> int:
        """Compute stats for every activity.

        Parameters
        ----------
        force:
            If *True*, recompute even when a row already exists.

        Returns the number of activities processed.
        """
        processed = 0
        with self.db.Session() as session:
            # Build a query that either includes all activities (force=True)
            # or only activities that do not yet have an ActivityStats row.
            if force:
                query = session.query(Activity).order_by(Activity.start_time)
            else:
                query = (
                    session.query(Activity)
                    .outerjoin(ActivityStats, ActivityStats.activity_id == Activity.id)
                    .filter(ActivityStats.activity_id.is_(None))
                    .order_by(Activity.start_time)
                )

            for act in query.yield_per(1000):
                row = self._build_stats_row(session, act)
                if row is None:
                    continue
                self._upsert(session, row)
                processed += 1

            session.commit()
        if processed:
            logger.info(f"StatsCalculator.compute_all: processed {processed} activities")
        return processed

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_stats_row(self, session: Session, act: Activity) -> ActivityStats | None:
        """Return an *unsaved* ActivityStats for *act* (None if sport unknown)."""

        # ---- Timing ----
        start = act.start_time
        if start.tzinfo is None:
            start = start.replace(tzinfo=ZoneInfo("UTC"))

        if act.end_time:
            end = act.end_time
            if end.tzinfo is None:
                end = end.replace(tzinfo=ZoneInfo("UTC"))
            duration_s = max(0, int((end - start).total_seconds()))
        else:
            end = None
            duration_s = 0

        # ---- Raw data ----
        hrs: list[HeartRate] = (
            session.query(HeartRate)
            .filter_by(activity_id=act.id)
            .order_by(HeartRate.timestamp_ms)
            .all()
        )
        runs: list[RunningMetrics] = (
            session.query(RunningMetrics)
            .filter_by(activity_id=act.id)
            .order_by(RunningMetrics.timestamp_ms)
            .all()
        )
        cycles: list[CyclingMetrics] = (
            session.query(CyclingMetrics)
            .filter_by(activity_id=act.id)
            .order_by(CyclingMetrics.timestamp_ms)
            .all()
        )

        # ---- Sport type ----
        sport_row = session.query(ActivitySport).filter_by(activity_id=act.id).first()
        if sport_row:
            sport = SportTypesEnum(sport_row.sport_type_id)
        else:
            sport = infer_sport(hrs, runs, cycles, act.id)

        if sport == SportTypesEnum.unknown:
            logger.debug(f"Skipping activity {act.id}: unknown sport")
            return None

        # ---- HR stats ----
        if hrs:
            bpms = [h.bpm for h in hrs]
            avg_bpm: float | None = sum(bpms) / len(bpms)
            max_bpm: int | None = max(bpms)
            total_kj = sum(h.energy_kj or 0.0 for h in hrs)
        else:
            avg_bpm = None
            max_bpm = None
            total_kj = 0.0

        # ---- Sport-specific stats ----
        if sport == SportTypesEnum.running and runs:
            primary = runs
            cadence_vals = [float(r.cadence_spm) for r in runs if r.cadence_spm is not None]
            power_vals = [float(r.power_watts) for r in runs if r.power_watts is not None]
        elif sport == SportTypesEnum.biking and cycles:
            primary = cycles
            cadence_vals = [float(c.cadence_rpm) for c in cycles if c.cadence_rpm is not None]
            power_vals = [float(c.power_watts) for c in cycles if c.power_watts is not None]
        else:
            primary = []
            cadence_vals = []
            power_vals = []

        distance_m = _last_distance(primary)
        avg_cadence = _safe_avg(cadence_vals)
        avg_power = _safe_avg(power_vals)

        # Average speed from distance / duration (avoids storing redundant value
        # but is cheap to pre-compute so the UI doesn't have to).
        if distance_m and duration_s > 0:
            avg_speed_mps: float | None = distance_m / duration_s
        else:
            avg_speed_mps = None

        # ---- Elevation ----
        ascent, descent = _calc_elevation(primary)

        return ActivityStats(
            activity_id=act.id,
            sport_type_id=sport.value,
            start_time=start,
            end_time=end,
            duration_s=duration_s,
            distance_m=distance_m,
            avg_speed_mps=avg_speed_mps,
            avg_bpm=avg_bpm,
            max_bpm=max_bpm,
            total_energy_kj=total_kj,
            avg_cadence=avg_cadence,
            avg_power_watts=avg_power,
            total_ascent_m=ascent if ascent > 0 else None,
            total_descent_m=descent if descent > 0 else None,
            computed_at=datetime.now(tz=ZoneInfo("UTC")),
        )

    @staticmethod
    def _upsert(session: Session, row: ActivityStats) -> None:
        """Insert or update the stats row for row.activity_id."""
        existing = session.query(ActivityStats).filter_by(activity_id=row.activity_id).one_or_none()
        if existing is None:
            session.add(row)
        else:
            # Update all mutable fields in-place so the session tracks the change.
            existing.sport_type_id = row.sport_type_id
            existing.start_time = row.start_time
            existing.end_time = row.end_time
            existing.duration_s = row.duration_s
            existing.distance_m = row.distance_m
            existing.avg_speed_mps = row.avg_speed_mps
            existing.avg_bpm = row.avg_bpm
            existing.max_bpm = row.max_bpm
            existing.total_energy_kj = row.total_energy_kj
            existing.avg_cadence = row.avg_cadence
            existing.avg_power_watts = row.avg_power_watts
            existing.total_ascent_m = row.total_ascent_m
            existing.total_descent_m = row.total_descent_m
            existing.computed_at = row.computed_at

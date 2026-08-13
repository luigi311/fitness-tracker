"""Typed SQLAlchemy models used by the fitness tracker."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from fitness_tracker.core.sports import SportTypesEnum


class Base(DeclarativeBase):
    """Declarative base for the application's database tables."""


class Activity(Base):
    """A recorded workout session."""

    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, default=uuid4)
    # timezone-aware UTC
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Wall-clock timestamps are not identity: two activities may start together.
    __table_args__ = (UniqueConstraint("public_id", name="uq_activities_public_id"),)

    heart_rates: Mapped[list[HeartRate]] = relationship(
        back_populates="activity",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    running_metrics: Mapped[list[RunningMetrics]] = relationship(
        back_populates="activity",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    cycling_metrics: Mapped[list[CyclingMetrics]] = relationship(
        back_populates="activity",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    uploads: Mapped[list[ActivityUpload]] = relationship(
        back_populates="activity",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ActivitySport(Base):
    """Link an activity to its sport type."""

    __tablename__ = "activity_sport"

    id: Mapped[int] = mapped_column(primary_key=True)
    activity_id: Mapped[int] = mapped_column(
        ForeignKey("activities.id", ondelete="CASCADE"),
        nullable=False,
    )
    sport_type_id: Mapped[int] = mapped_column(Integer, nullable=False)

    # ensure one row per activity (enforce 1:1 relationship)
    __table_args__ = (UniqueConstraint("activity_id", name="uq_activity_sport_activity_id"),)


class HeartRate(Base):
    """Heart-rate samples recorded during an activity."""

    __tablename__ = "heart_rate"

    id: Mapped[int] = mapped_column(primary_key=True)
    activity_id: Mapped[int] = mapped_column(
        ForeignKey("activities.id", ondelete="CASCADE"),
        nullable=False,
    )
    timestamp_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bpm: Mapped[int] = mapped_column(Integer, nullable=False)
    rr_interval: Mapped[float | None] = mapped_column(Float)
    energy_kj: Mapped[float | None] = mapped_column(Float)

    activity: Mapped[Activity] = relationship(back_populates="heart_rates")

    # index for quick lookups by activity, and by activity+time
    __table_args__ = (
        Index("ix_hr_activity_id", "activity_id"),
        Index("ix_hr_activity_time", "activity_id", "timestamp_ms"),
    )


class RunningMetrics(Base):
    """Running sensor metrics recorded during an activity."""

    __tablename__ = "running_metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    activity_id: Mapped[int] = mapped_column(
        ForeignKey("activities.id", ondelete="CASCADE"),
        nullable=False,
    )
    timestamp_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # core metrics
    speed_mps: Mapped[float] = mapped_column(Float, nullable=False)  # speed (m/s)
    cadence_spm: Mapped[int] = mapped_column(Integer, nullable=False)  # steps per minute
    stride_length_m: Mapped[float | None] = mapped_column(Float)
    total_distance_m: Mapped[float | None] = mapped_column(Float)
    power_watts: Mapped[float | None] = mapped_column(Float)
    incline_percent: Mapped[float | None] = mapped_column(Float)
    altitude_m: Mapped[float | None] = mapped_column(Float)

    activity: Mapped[Activity] = relationship(back_populates="running_metrics")

    # indexes to query by activity and time
    __table_args__ = (
        Index("ix_run_activity_id", "activity_id"),
        Index("ix_run_activity_time", "activity_id", "timestamp_ms"),
    )


class CyclingMetrics(Base):
    """Cycling sensor metrics recorded during an activity."""

    __tablename__ = "cycling_metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    activity_id: Mapped[int] = mapped_column(
        ForeignKey("activities.id", ondelete="CASCADE"),
        nullable=False,
    )
    timestamp_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)

    speed_mps: Mapped[float] = mapped_column(Float, nullable=False)  # speed (m/s)
    cadence_rpm: Mapped[int | None] = mapped_column(Integer)  # revolutions per minute
    total_distance_m: Mapped[float | None] = mapped_column(Float)
    power_watts: Mapped[float | None] = mapped_column(Float)
    incline_percent: Mapped[float | None] = mapped_column(Float)
    altitude_m: Mapped[float | None] = mapped_column(Float)

    activity: Mapped[Activity] = relationship(back_populates="cycling_metrics")

    __table_args__ = (
        Index("ix_cyc_activity_id", "activity_id"),
        Index("ix_cyc_activity_time", "activity_id", "timestamp_ms"),
    )


class ActivityUpload(Base):
    """Upload status for an activity and external provider."""

    __tablename__ = "activity_uploads"

    id: Mapped[int] = mapped_column(primary_key=True)
    activity_id: Mapped[int] = mapped_column(
        ForeignKey("activities.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. "intervals_icu"
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
    )  # "pending"|"ok"|"failed"
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    provider_activity_id: Mapped[str | None] = mapped_column(String(128))
    payload_hash: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)

    activity: Mapped[Activity] = relationship(back_populates="uploads")

    __table_args__ = (
        UniqueConstraint("activity_id", "provider", name="uq_activity_provider"),
        Index("ix_upload_provider_status", "provider", "status"),
        Index("ix_upload_activity", "activity_id"),
    )


class ActivityStats(Base):
    """Flat, pre-computed summary row for a single activity."""

    __tablename__ = "activity_stats"

    id: Mapped[int] = mapped_column(primary_key=True)
    activity_id: Mapped[int] = mapped_column(
        ForeignKey("activities.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Sport
    sport_type_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=SportTypesEnum.unknown.value,
    )

    # Timing (stored as timezone-aware UTC datetimes; UI is free to localise)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_s: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Distance / pace / speed
    distance_m: Mapped[float | None] = mapped_column(Float)
    avg_speed_mps: Mapped[float | None] = mapped_column(Float)

    # Heart rate
    avg_bpm: Mapped[float | None] = mapped_column(Float)
    max_bpm: Mapped[int | None] = mapped_column(Integer)
    # Existing databases have this NOT NULL column. Keep a private mapping so
    # new stats rows remain insertable while energy stays unused by the app.
    _legacy_total_energy_kj: Mapped[float] = mapped_column(
        "total_energy_kj",
        Float,
        nullable=False,
        default=0.0,
    )

    @property
    def total_energy_kj(self) -> float:
        """Expose the retained legacy energy column to existing readers."""
        return self._legacy_total_energy_kj

    # Cadence / power
    avg_cadence: Mapped[float | None] = mapped_column(Float)
    avg_power_watts: Mapped[float | None] = mapped_column(Float)

    # Elevation
    total_ascent_m: Mapped[float | None] = mapped_column(Float)
    total_descent_m: Mapped[float | None] = mapped_column(Float)

    # Computed at
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("activity_id", name="uq_activity_stats_activity_id"),
        Index("ix_stats_activity_id", "activity_id"),
        Index("ix_stats_start_time", "start_time"),
        Index("ix_stats_sport", "sport_type_id"),
    )

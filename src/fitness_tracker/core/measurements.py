"""Persistence-ready measurement value objects."""

from pydantic import BaseModel, ConfigDict


class NormalizedHeartRate(BaseModel):
    """A heart-rate sample with explicit units."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timestamp_ms: int
    bpm: int
    rr_interval_ms: float | None = None

"""Latest canonical sensor values shared by sampling and rendering paths."""

from pydantic import BaseModel, ConfigDict


class LiveMetrics(BaseModel):
    """The most recent live measurements awaiting display."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    heart_rate_bpm: int = 0
    speed_mps: float = 0.0
    cadence_spm: int = 0
    distance_m: float = 0.0
    power_watts: int = 0

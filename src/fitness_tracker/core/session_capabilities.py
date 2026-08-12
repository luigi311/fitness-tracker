"""Capabilities resolved for a session view."""

from pydantic import BaseModel, ConfigDict


class SessionCapabilities(BaseModel):
    """Describe optional controls and live surfaces enabled for a session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incline: bool
    trainer_targets: bool

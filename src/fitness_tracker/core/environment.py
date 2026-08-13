"""Typed environment specifications for session selection."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from fitness_tracker.core.sports import SportTypesEnum


class Environment(StrEnum):
    """Supported session environments."""

    INDOOR = "indoor"
    OUTDOOR = "outdoor"
    TRAINER = "trainer"

    @property
    def uses_trainer(self) -> bool:
        """Return whether this environment requires a smart trainer."""
        return self is Environment.TRAINER


class Badge(BaseModel):
    """Feature badge displayed on an environment card."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    css_class: str
    sport_type: SportTypesEnum | None = None


class EnvironmentSpec(BaseModel):
    """Display metadata for one environment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    icon: str
    label: str
    badges: tuple[Badge, ...] = ()

"""Trainer target mode identifiers."""

from collections.abc import Iterable
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from fitness_tracker.core.sports import SportTypesEnum


class TrainerMode(StrEnum):
    """Supported trainer target controls."""

    BIAS = "Bias"
    POWER = "Power"
    RESISTANCE = "Resistance"
    SPEED = "Speed"
    HEART_RATE = "HeartRate"


def trainer_modes_for_session(
    sport_type: SportTypesEnum,
    *,
    include_bias: bool,
) -> tuple[TrainerMode, ...]:
    """Return the trainer target modes for a free or workout session."""
    sport_modes = (
        (TrainerMode.POWER, TrainerMode.RESISTANCE)
        if sport_type is SportTypesEnum.biking
        else (TrainerMode.SPEED,)
    )
    if include_bias:
        return (TrainerMode.BIAS, *sport_modes)
    return sport_modes


def fallback_trainer_mode(
    available_modes: Iterable[TrainerMode],
    *,
    current: TrainerMode,
    unavailable: TrainerMode,
) -> TrainerMode | None:
    """Return the first remaining mode when the active mode is hidden."""
    if current is not unavailable:
        return None
    return next(
        (mode for mode in available_modes if mode is not unavailable),
        None,
    )


class TrainerModeConfig(BaseModel):
    """Validated numeric configuration for one trainer mode."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    minimum: float
    maximum: float
    step: float
    unit: str
    decimals: int = 0

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        if self.minimum >= self.maximum:
            message = "minimum must be below maximum"
            raise ValueError(message)
        return self

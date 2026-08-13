"""Validated sensor profiles shared by the UI and recorder."""

from typing import Self

from pydantic import BaseModel, ConfigDict

from fitness_tracker.core.settings import SensorSettings, TrainerSettings


class SensorProfile(BaseModel):
    """Complete sensor selection for one recording profile."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hr_name: str | None = None
    hr_address: str | None = None
    trainer_supplied_hr: bool = False
    speed_name: str | None = None
    speed_address: str | None = None
    cadence_name: str | None = None
    cadence_address: str | None = None
    power_name: str | None = None
    power_address: str | None = None
    trainer_name: str | None = None
    trainer_address: str | None = None
    trainer_machine_type: int | None = None

    @classmethod
    def from_sensor_settings(cls, section: SensorSettings) -> Self:
        """Build a profile from a regular sensor settings section."""
        return cls(
            hr_name=section.hr_name,
            hr_address=section.hr_address,
            speed_name=section.speed_name,
            speed_address=section.speed_address,
            cadence_name=section.cadence_name,
            cadence_address=section.cadence_address,
            power_name=section.power_name,
            power_address=section.power_address,
        )

    @classmethod
    def from_trainer_settings(cls, section: TrainerSettings) -> Self:
        """Build a profile from a trainer sensor settings section."""
        return cls(
            hr_name=None if section.trainer_supplied_hr else section.hr_name,
            hr_address=None if section.trainer_supplied_hr else section.hr_address,
            trainer_supplied_hr=section.trainer_supplied_hr,
            trainer_name=section.trainer_name,
            trainer_address=section.trainer_address,
            trainer_machine_type=section.trainer_machine_type,
        )

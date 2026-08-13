"""Compatibility facade for the legacy UI during the layered migration."""

from collections.abc import Callable

from bleaksport import (
    HeartRateSample,
    MachineType,
    RunningSample,
    TrainerMux,
    TrainerSample,
)
from bleaksport.models import CyclingSample

from fitness_tracker.core.sensor_profile import SensorProfile
from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.database import DatabaseManager
from fitness_tracker.hardware.recorder import Recorder as HardwareRecorder


class Recorder(HardwareRecorder):
    """Adapt the legacy constructor and attributes to the hardware recorder."""

    def __init__(
        self,
        weight_kg: float | None,
        sport_type: SportTypesEnum,
        database_url: str,
        hr_name: str | None,
        hr_address: str | None,
        speed_name: str | None,
        speed_address: str | None,
        cadence_name: str | None,
        cadence_address: str | None,
        power_name: str | None,
        power_address: str | None,
        trainer_name: str | None,
        trainer_address: str | None,
        trainer_machine_type: MachineType | None,
        on_error: Callable[[str], None],
        *,
        on_sample_update: Callable[
            [CyclingSample | HeartRateSample | RunningSample | TrainerSample],
            None,
        ]
        | None = None,
        test_mode: bool = False,
        trainer_supplied_hr: bool = False,
    ) -> None:
        profile = SensorProfile(
            hr_name=hr_name,
            hr_address=hr_address,
            trainer_supplied_hr=trainer_supplied_hr,
            speed_name=speed_name,
            speed_address=speed_address,
            cadence_name=cadence_name,
            cadence_address=cadence_address,
            power_name=power_name,
            power_address=power_address,
            trainer_name=trainer_name,
            trainer_address=trainer_address,
            trainer_machine_type=(
                int(trainer_machine_type) if trainer_machine_type is not None else None
            ),
        )
        database = DatabaseManager(database_url=database_url)
        super().__init__(
            profile=profile,
            weight_kg=weight_kg,
            sport_type=sport_type,
            database=database,
            on_error=on_error,
            on_sample_update=on_sample_update,
            test_mode=test_mode,
        )

        # Legacy pages still query through the recorder until the next merge unit.
        self.db = database
        self.stat_calc = database.stat_calc

    @property
    def hr_address(self) -> str | None:
        return self.profile.hr_address

    @property
    def speed_address(self) -> str | None:
        return self.profile.speed_address

    @property
    def cadence_address(self) -> str | None:
        return self.profile.cadence_address

    @property
    def power_address(self) -> str | None:
        return self.profile.power_address

    @property
    def trainer_address(self) -> str | None:
        return self.profile.trainer_address

    @property
    def trainer_machine_type(self) -> int | None:
        return self.profile.trainer_machine_type

    @property
    def trainer_mux(self) -> TrainerMux | None:
        return self.trainer.trainer_mux

"""Connection status values shared by session views."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class SensorKind(StrEnum):
    """Sensor identities used by metric tiles and connection status."""

    HEART_RATE = "HR"
    SPEED = "Speed"
    CADENCE = "Cadence"
    POWER = "Power"
    DISTANCE = "Distance"

    def tooltip(self, *, connected: bool) -> str:
        """Return the connection tooltip for this sensor."""
        state = "connected" if connected else "not connected"
        return f"{self.value} sensor {state}"


class SensorStatus(BaseModel):
    """Describe which live measurement sources are currently connected."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hr: bool = False
    speed: bool = False
    cadence: bool = False
    power: bool = False
    distance: bool = False

    def is_connected(self, kind: SensorKind) -> bool:
        """Return whether the sensor identified by ``kind`` is connected."""
        match kind:
            case SensorKind.HEART_RATE:
                return self.hr
            case SensorKind.SPEED:
                return self.speed
            case SensorKind.CADENCE:
                return self.cadence
            case SensorKind.POWER:
                return self.power
            case SensorKind.DISTANCE:
                return self.distance

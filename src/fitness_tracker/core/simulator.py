"""Pure test-mode sensor simulation."""

from __future__ import annotations

from math import exp
from random import Random

from pydantic import BaseModel, ConfigDict

from fitness_tracker.core.units import mps_from_mph

_HIGH_POWER_THRESHOLD_W = 300.0
_INITIAL_SPEED_MPS = mps_from_mph(6.8)
_MIN_SPEED_MPS = mps_from_mph(2.0)
_MAX_SPEED_MPS = mps_from_mph(10.0)
_TARGET_SPEED_NOISE_MPS = mps_from_mph(0.05)
_FREE_SPEED_NOISE_MPS = mps_from_mph(0.3)
_POWER_WAVE_PERIOD_S = 120.0
_POWER_WAVE_PHASE_ONE_END_S = 30.0
_POWER_WAVE_PHASE_TWO_END_S = 60.0
_POWER_WAVE_PHASE_THREE_END_S = 90.0


class SimulationTarget(BaseModel):
    """Optional workout targets that influence the simulated sensor reading."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    power_watts: float | None = None
    speed_mps: float | None = None
    heart_rate_bpm: float | None = None


class SimulatedReading(BaseModel):
    """A simulated reading in the application's canonical sensor units."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    heart_rate_bpm: int
    speed_mps: float
    cadence_spm: int
    distance_m: float
    power_watts: int


class SensorSimulator:
    """Generate stateful heart-rate, motion, distance, and power readings."""

    def __init__(
        self,
        *,
        resting_hr: float,
        max_hr: float,
        low_hr: float,
        rng: Random | None = None,
    ) -> None:
        self._resting_hr = float(resting_hr)
        self._max_hr = float(max_hr)
        self._low_hr = float(low_hr)
        self._rng = rng or Random()
        self.reset()

    def reset(self) -> None:
        """Reset simulated physiology and accumulated motion for a new session."""
        self._elapsed_s = 0.0
        self._power_watts = 250.0
        self._speed_mps = _INITIAL_SPEED_MPS
        self._cadence_spm = 86
        self._distance_m = 0.0
        self._heart_rate_bpm = self._resting_hr

    def tick(
        self,
        dt_s: float,
        target: SimulationTarget | None = None,
        *,
        active: bool = True,
    ) -> SimulatedReading:
        """Advance the simulation and return one canonical-unit reading."""
        dt_s = max(0.001, float(dt_s))
        if active:
            self._elapsed_s += dt_s

        if target is None:
            target_power = self._free_run_power_target()
        elif target.power_watts is not None:
            target_power = target.power_watts
        else:
            target_power = self._power_watts
        self._power_watts += 0.35 * (target_power - self._power_watts)

        if target is not None and target.heart_rate_bpm is not None:
            hr_target = target.heart_rate_bpm
            tau = 20.0
        elif self._power_watts > _HIGH_POWER_THRESHOLD_W:
            hr_target = self._max_hr - self._rng.uniform(0, 3)
            tau = 10.0
        else:
            hr_target = self._low_hr - self._rng.uniform(0, 5)
            tau = 45.0
        alpha = 1.0 - exp(-dt_s / tau)
        self._heart_rate_bpm += alpha * (hr_target - self._heart_rate_bpm)
        self._heart_rate_bpm += self._rng.uniform(-0.8, 0.8)
        self._heart_rate_bpm = max(
            self._resting_hr,
            min(self._max_hr, self._heart_rate_bpm),
        )

        target_speed_mps = target.speed_mps if target is not None else None
        if target_speed_mps is not None:
            self._speed_mps += 0.25 * (target_speed_mps - self._speed_mps)
            self._speed_mps += self._rng.uniform(
                -_TARGET_SPEED_NOISE_MPS,
                _TARGET_SPEED_NOISE_MPS,
            )
            self._speed_mps = max(
                _MIN_SPEED_MPS,
                min(_MAX_SPEED_MPS, self._speed_mps),
            )
        else:
            self._speed_mps = max(
                _MIN_SPEED_MPS,
                min(
                    _MAX_SPEED_MPS,
                    self._speed_mps
                    + self._rng.uniform(-_FREE_SPEED_NOISE_MPS, _FREE_SPEED_NOISE_MPS),
                ),
            )

        self._cadence_spm = int(
            max(75, min(95, self._cadence_spm + self._rng.uniform(-2, 2))),
        )
        if active:
            self._distance_m += self._speed_mps * dt_s

        return SimulatedReading(
            heart_rate_bpm=round(self._heart_rate_bpm),
            speed_mps=self._speed_mps,
            cadence_spm=self._cadence_spm,
            distance_m=self._distance_m,
            power_watts=int(self._power_watts),
        )

    def _free_run_power_target(self) -> float:
        """Return the current free-run power-wave target."""
        phase = self._elapsed_s % _POWER_WAVE_PERIOD_S
        if phase < _POWER_WAVE_PHASE_ONE_END_S:
            return self._rng.uniform(400, 600)
        if phase < _POWER_WAVE_PHASE_TWO_END_S:
            return self._rng.uniform(180, 250)
        if phase < _POWER_WAVE_PHASE_THREE_END_S:
            return self._rng.uniform(350, 550)
        return self._rng.uniform(180, 240)

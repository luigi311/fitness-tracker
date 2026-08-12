"""Translate resolved workout guidance into trainer target commands."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Protocol

from fitness_tracker.core.guidance import StepGuidance, TargetDomain
from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.core.trainer_mode import TrainerMode
from fitness_tracker.core.units import UnitSystem, speed_in_units

if TYPE_CHECKING:
    from fitness_tracker.core.throttle import TrainerTargetThrottle


class TrainerTargetSink(Protocol):
    """Recorder surface needed by the guidance adapter."""

    @property
    def trainer_connected(self) -> bool:
        """Return whether the trainer transport is connected."""
        ...

    @property
    def trainer_heart_rate_control_available(self) -> bool:
        """Return whether trainer-controlled heart-rate targets are supported."""
        ...

    def set_target_power(self, watts: int) -> None:
        """Request a power target in watts."""
        ...

    def set_target_speed(self, speed_kmh: float) -> None:
        """Request a speed target in kilometres per hour."""
        ...

    def set_target_heart_rate(self, bpm: int) -> bool:
        """Request a heart-rate target and report whether it was accepted."""
        ...


def apply_trainer_guidance(
    guidance: StepGuidance,
    *,
    target_sink: TrainerTargetSink | None,
    trainer_mode: TrainerMode,
    trainer_session: bool,
    sport_type: SportTypesEnum,
    throttle: TrainerTargetThrottle,
) -> None:
    """Send a target when the trainer and throttle policy permit it."""
    if (
        target_sink is None
        or not target_sink.trainer_connected
        or trainer_mode is not TrainerMode.BIAS
    ):
        return

    now = time.monotonic()
    if guidance.domain is TargetDomain.POWER:
        target_watts = round(guidance.mid)
        if throttle.should_send(TrainerMode.POWER, target_watts, now):
            target_sink.set_target_power(target_watts)
            throttle.mark_sent(TrainerMode.POWER, target_watts, now)
        return

    if (
        guidance.domain is TargetDomain.PACE
        and trainer_session
        and sport_type is SportTypesEnum.running
    ):
        target_kmh = round(
            speed_in_units(guidance.mid, UnitSystem.METRIC),
            1,
        )
        if throttle.should_send(TrainerMode.SPEED, target_kmh, now):
            target_sink.set_target_speed(target_kmh)
            throttle.mark_sent(TrainerMode.SPEED, target_kmh, now)
        return

    if guidance.domain is TargetDomain.HEART_RATE:
        if not target_sink.trainer_heart_rate_control_available:
            return
        target_bpm = round(guidance.mid)
        if throttle.should_send(
            TrainerMode.HEART_RATE,
            target_bpm,
            now,
        ) and target_sink.set_target_heart_rate(target_bpm):
            throttle.mark_sent(TrainerMode.HEART_RATE, target_bpm, now)

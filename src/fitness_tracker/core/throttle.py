"""Trainer command throttling shared by the session controller."""

from typing import Final

from fitness_tracker.core.trainer_mode import TrainerMode

_POWER_MIN_DELTA_WATTS: Final = 3.0
_COMMAND_MIN_INTERVAL_S: Final = 2.0
_HEART_RATE_REFRESH_INTERVAL_S: Final = 10.0


class TrainerTargetThrottle:
    """Rate-limit repeated trainer commands while allowing mode changes immediately."""

    def __init__(self) -> None:
        self._last_kind: TrainerMode | None = None
        self._last_value: float | None = None
        self._last_sent_at = 0.0

    def should_send(
        self,
        kind: TrainerMode,
        value: float,
        now: float,
    ) -> bool:
        """Return whether ``value`` should be sent at ``now``."""
        if self._last_kind is not kind or self._last_value is None:
            return True

        elapsed = now - self._last_sent_at
        match kind:
            case TrainerMode.POWER:
                return (
                    abs(self._last_value - value) >= _POWER_MIN_DELTA_WATTS
                    and elapsed > _COMMAND_MIN_INTERVAL_S
                )
            case TrainerMode.SPEED:
                return self._last_value != value and elapsed > _COMMAND_MIN_INTERVAL_S
            case TrainerMode.HEART_RATE:
                return self._last_value != value or elapsed >= _HEART_RATE_REFRESH_INTERVAL_S
            case TrainerMode.BIAS | TrainerMode.RESISTANCE:
                message = f"{kind} is not a throttled trainer command"
                raise ValueError(message)

    def mark_sent(
        self,
        kind: TrainerMode,
        value: float,
        now: float,
    ) -> None:
        """Record a command after it has been accepted by the recorder."""
        self._last_kind = kind
        self._last_value = value
        self._last_sent_at = now

    def reset(self) -> None:
        """Forget the last command so the next target is sent immediately."""
        self._last_kind = None
        self._last_value = None
        self._last_sent_at = 0.0

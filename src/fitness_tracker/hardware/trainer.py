"""Trainer target control and ERG safety state."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

from loguru import logger

from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.core.trainer_mode import TrainerMode

if TYPE_CHECKING:
    from collections.abc import Callable
    from concurrent.futures import Future

    from bleaksport import TrainerMux, TrainerSample


class TrainerController:
    """Manage trainer targets, retries, and the ERG recovery safeguard.

    ERG starts disabled deliberately. It remains disabled until the safeguard
    observes sustained power above its recovery threshold, preventing a newly
    connected trainer from applying a demanding target before the rider is ready.
    """

    def __init__(
        self,
        *,
        sport_type: SportTypesEnum,
        trainer_supplied_hr: bool,
        loop: asyncio.AbstractEventLoop,
        on_error: Callable[[str], None],
        test_mode: bool,
    ) -> None:
        self.sport_type = sport_type
        self.trainer_supplied_hr = trainer_supplied_hr
        self.loop = loop
        self.on_error = on_error
        self.test_mode = test_mode
        self._target_lock = threading.RLock()
        self._trainer_mux: TrainerMux | None = None
        self._pending_target: float | None = None
        self._target_mode: TrainerMode | None = None
        self.erg_retry_task: Future | None = None
        self._erg_applied_target: float | None = None

        self._power_above_since_ms: int | None = None
        self._power_below_since_ms: int | None = None
        self._erg_disabled = True
        self.erg_safeguard_saved_watts: int | None = None
        self._trainer_heart_rate_available = False

    @property
    def trainer_mux(self) -> TrainerMux | None:
        """Return the trainer mux under the target-state lock."""
        with self._target_lock:
            return self._trainer_mux

    @trainer_mux.setter
    def trainer_mux(self, value: TrainerMux | None) -> None:
        with self._target_lock:
            if value is self._trainer_mux:
                return
            self._trainer_mux = value
            self._trainer_heart_rate_available = False
            self._cancel_heart_rate_target_locked()

    @property
    def pending_target(self) -> float | None:
        """Return the target waiting to be sent."""
        with self._target_lock:
            return self._pending_target

    @pending_target.setter
    def pending_target(self, value: float | None) -> None:
        with self._target_lock:
            self._pending_target = value

    @property
    def target_mode(self) -> TrainerMode | None:
        """Return the active trainer target mode."""
        with self._target_lock:
            return self._target_mode

    @target_mode.setter
    def target_mode(self, value: TrainerMode | None) -> None:
        with self._target_lock:
            self._target_mode = value

    @property
    def erg_applied_target(self) -> float | None:
        """Return the last target acknowledged by the trainer."""
        with self._target_lock:
            return self._erg_applied_target

    @erg_applied_target.setter
    def erg_applied_target(self, value: float | None) -> None:
        with self._target_lock:
            self._erg_applied_target = value

    @property
    def erg_disabled(self) -> bool:
        """Return whether the ERG safeguard currently blocks power targets."""
        with self._target_lock:
            return self._erg_disabled

    @erg_disabled.setter
    def erg_disabled(self, value: bool) -> None:
        with self._target_lock:
            self._erg_disabled = value

    def shutdown(self) -> None:
        """Clear pending trainer work and cancel an active retry loop."""
        with self._target_lock:
            self.pending_target = None
            if self.erg_retry_task and not self.erg_retry_task.done():
                self.erg_retry_task.cancel()
            self.erg_retry_task = None

    def on_link(self, *, connected: bool) -> None:
        """Reset connection-sensitive target state when the trainer disconnects."""
        if connected:
            return

        with self._target_lock:
            self._trainer_heart_rate_available = False
            self.erg_disabled = True
            self.erg_safeguard_saved_watts = None
            self._power_above_since_ms = None
            self._power_below_since_ms = None
            if self.target_mode is TrainerMode.HEART_RATE:
                self.pending_target = None
                if self.erg_retry_task and not self.erg_retry_task.done():
                    self.erg_retry_task.cancel()
                self.erg_retry_task = None
            # Reset ERG watts on disconnect so it applies immediately on reconnect.
            self.erg_applied_target = None

    def neutralize(self) -> None:
        """Apply zero trainer load without normal target gating."""
        with self._target_lock:
            self.erg_disabled = True
            self.erg_safeguard_saved_watts = None
            self._power_above_since_ms = None
            self._power_below_since_ms = None
            if self.sport_type is SportTypesEnum.biking:
                self._set_target_resistance(0, preserve_erg_recovery=False)
            else:
                self.set_target_speed(0)

    def handle_sample(self, sample: TrainerSample) -> None:
        """Process trainer feedback and reconcile an externally changed target."""
        self.update_erg_safeguard(sample.timestamp_ms, sample.power_watts)

        with self._target_lock:
            if (
                sample.target_power is not None
                and self.pending_target is None
                and self.erg_applied_target != sample.target_power
                and self.target_mode in (None, TrainerMode.POWER)
            ):
                logger.debug(
                    f"Trainer target power {sample.target_power} watts differs from applied "
                    f"{self.erg_applied_target} watts, scheduling update",
                )
                self._select_target_mode(TrainerMode.POWER)
                self.pending_target = sample.target_power
                self._ensure_retry_loop(TrainerMode.POWER)

    def handle_embedded_heart_rate(self, bpm: float | None) -> bool:
        """Record trainer HR availability and return whether the value is usable."""
        with self._target_lock:
            if not self.trainer_supplied_hr or bpm is None:
                return False
            self._trainer_heart_rate_available = True
            return True

    def set_target_power(self, watts: int) -> None:
        """Set target power on the trainer if supported."""
        logger.debug(f"Trying to set target power to {watts} watts")

        with self._target_lock:
            if self.erg_disabled:
                # Safeguard active — stash for when it lifts.
                logger.debug(f"Erg is currently disabled, stashing {watts} target")
                self.erg_safeguard_saved_watts = watts
                return

            self._select_target_mode(TrainerMode.POWER)
            self.pending_target = watts
            if self.erg_applied_target != watts:
                self.erg_applied_target = None
            self._ensure_retry_loop(TrainerMode.POWER)

    def set_target_resistance(self, resistance: float) -> None:
        """Set target resistance on the trainer if supported."""
        logger.debug(f"Trying to set target resistance to {resistance}")
        self._set_target_resistance(resistance, preserve_erg_recovery=False)

    def _set_target_resistance(
        self,
        resistance: float,
        *,
        preserve_erg_recovery: bool,
    ) -> None:
        """Set resistance, optionally retaining watts for safeguard recovery."""
        with self._target_lock:
            if not preserve_erg_recovery:
                self.erg_safeguard_saved_watts = None
            self._select_target_mode(TrainerMode.RESISTANCE)
            self.pending_target = resistance
            if self.erg_applied_target != resistance:
                self.erg_applied_target = None
            self._ensure_retry_loop(TrainerMode.RESISTANCE)

    def set_target_speed(self, speed_kmh: float) -> None:
        """Set target treadmill speed in kilometers per hour."""
        logger.debug(f"Trying to set target speed to {speed_kmh} km/h")
        with self._target_lock:
            self.erg_safeguard_saved_watts = None
            self._select_target_mode(TrainerMode.SPEED)
            self.pending_target = speed_kmh
            if self.erg_applied_target != speed_kmh:
                self.erg_applied_target = None
            self._ensure_retry_loop(TrainerMode.SPEED)

    def set_target_heart_rate(self, bpm: int) -> bool:
        """Set trainer-controlled target HR when its own HR telemetry is available."""
        with self._target_lock:
            if not self._trainer_heart_rate_control_available_locked():
                logger.debug(
                    "Ignoring target heart rate because trainer-supplied HR is not available",
                )
                return False

            bpm = int(bpm)
            logger.debug(f"Trying to set target heart rate to {bpm} bpm")
            self.erg_safeguard_saved_watts = None
            self._select_target_mode(TrainerMode.HEART_RATE)
            self.pending_target = bpm
            if self.erg_applied_target != bpm:
                self.erg_applied_target = None
            self._ensure_retry_loop(TrainerMode.HEART_RATE)
        return True

    @property
    def trainer_heart_rate_control_available(self) -> bool:
        """Return whether trainer-controlled HR targets are currently safe to use."""
        with self._target_lock:
            return self._trainer_heart_rate_control_available_locked()

    def _trainer_heart_rate_control_available_locked(self) -> bool:
        return bool(
            self.trainer_supplied_hr
            and self._trainer_heart_rate_available
            and self._trainer_mux
            and self._trainer_mux.supports_target_heart_rate,
        )

    def _cancel_heart_rate_target_locked(self) -> None:
        if self._target_mode is not TrainerMode.HEART_RATE:
            return
        self._target_mode = None
        self._pending_target = None
        self._erg_applied_target = None
        if self.erg_retry_task and not self.erg_retry_task.done():
            self.erg_retry_task.cancel()
        self.erg_retry_task = None

    def _select_target_mode(self, target_mode: TrainerMode) -> None:
        with self._target_lock:
            if self.target_mode != target_mode:
                self.target_mode = target_mode
                self.pending_target = None
                self.erg_applied_target = None
                previous_retry = self.erg_retry_task
                self.erg_retry_task = None
                if previous_retry and not previous_retry.done():
                    previous_retry.cancel()

    def _ensure_retry_loop(self, target_mode: TrainerMode) -> None:
        with self._target_lock:
            self._select_target_mode(target_mode)

            if self.test_mode:
                return

            if self.erg_retry_task and not self.erg_retry_task.done():
                return

            self.erg_retry_task = asyncio.run_coroutine_threadsafe(
                self._retry_loop(target_mode),
                self.loop,
            )

    async def _retry_loop(self, target_mode: TrainerMode) -> None:
        retry_interval = 2.0
        while True:
            with self._target_lock:
                if self.target_mode != target_mode:
                    return
                # Snapshot state without holding the thread lock across BLE awaits.
                target = self.pending_target
                erg_disabled = self.erg_disabled
            if target is None:
                return

            if erg_disabled and target_mode is TrainerMode.POWER:
                logger.debug("Erg mode is currently disabled, skipping setting")
                await asyncio.sleep(retry_interval)
                continue

            mux = self.trainer_mux
            if mux and mux.is_connected:
                try:
                    result = await self._send_target(mux, target_mode, target)

                    # Only clear the pending value if it wasn't updated while
                    # the await was in flight.
                    with self._target_lock:
                        if self.target_mode == target_mode and self.pending_target == result:
                            self.pending_target = None
                            self.erg_applied_target = result
                            return

                except Exception as error:
                    self.on_error(f"ERG set failed, retrying: {error}")

            await asyncio.sleep(retry_interval)

    async def _send_target(
        self,
        mux: TrainerMux,
        target_mode: TrainerMode,
        target: float,
    ) -> int | float:
        if target_mode is TrainerMode.POWER:
            return await mux.set_target_power(int(target))
        if target_mode is TrainerMode.RESISTANCE:
            return await mux.set_target_resistance(float(target))
        if target_mode is TrainerMode.SPEED:
            return await mux.set_target_speed(float(target))
        if target_mode is TrainerMode.HEART_RATE:
            return await mux.set_target_heart_rate(int(target))
        message = "Bias is a UI-only trainer mode"
        raise ValueError(message)

    def _power_window_state(self, timestamp_ms: int, power: int) -> tuple[bool, bool]:
        """Update sustained-power markers and return below/above decisions."""
        window = 3000
        power_threshold = 60
        if power > power_threshold:
            if self._power_above_since_ms is None or timestamp_ms < self._power_above_since_ms:
                self._power_above_since_ms = timestamp_ms
            self._power_below_since_ms = None
        elif power < power_threshold:
            if self._power_below_since_ms is None or timestamp_ms < self._power_below_since_ms:
                self._power_below_since_ms = timestamp_ms
            self._power_above_since_ms = None
        else:
            self._power_above_since_ms = None
            self._power_below_since_ms = None

        all_below = (
            self._power_below_since_ms is not None
            and timestamp_ms - self._power_below_since_ms >= window
        )
        all_above = (
            self._power_above_since_ms is not None
            and timestamp_ms - self._power_above_since_ms >= window
        )
        return all_below, all_above

    def _disable_erg_for_low_power(self) -> None:
        """Save an active power target and switch to the recovery resistance."""
        with self._target_lock:
            if self.target_mode is not TrainerMode.POWER:
                return
            power_target = (
                self.pending_target if self.pending_target is not None else self.erg_applied_target
            )
            if power_target is not None:
                self.erg_safeguard_saved_watts = int(power_target)
            if not self.test_mode:
                self._set_target_resistance(5, preserve_erg_recovery=True)

    def _recover_erg_after_power(self) -> None:
        """Restore the saved power target after sustained power recovery."""
        if self.erg_safeguard_saved_watts is None:
            return
        self.set_target_power(self.erg_safeguard_saved_watts)
        self.erg_safeguard_saved_watts = None

    def update_erg_safeguard(self, timestamp_ms: int, power_watts: int | None) -> None:
        """Update the sustained-power state that gates ERG targets."""
        with self._target_lock:
            if (
                self.target_mode is not TrainerMode.POWER
                and self.sport_type != SportTypesEnum.biking
            ):
                return

            power = power_watts or 0
            logger.trace((timestamp_ms, power_watts))
            all_below, all_above = self._power_window_state(timestamp_ms, power)

            if all_below and not self.erg_disabled:
                self.erg_disabled = True
                logger.warning("ERG safeguard: power too low, disabling ERG")
                self._disable_erg_for_low_power()

            elif all_above and self.erg_disabled:
                self.erg_disabled = False
                logger.success("ERG safeguard: power recovered, re-enabling ERG")
                self._recover_erg_after_power()

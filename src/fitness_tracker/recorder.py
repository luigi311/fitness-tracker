import asyncio
import contextlib
import threading
from collections import deque
from collections.abc import Callable
from statistics import median
from typing import TYPE_CHECKING

import gi
from bleak import BleakScanner
from bleaksport import (
    CyclingMux,
    HeartRateMux,
    HeartRateSample,
    MachineType,
    RunningMux,
    RunningSample,
    TrainerMux,
    TrainerSample,
)
from bleaksport.models import CyclingSample
from loguru import logger

from fitness_tracker.activity_stats import StatsCalculator
from fitness_tracker.database import DatabaseManager, SportTypesEnum

gi.require_versions({"Gtk": "4.0", "Adw": "1"})

from gi.repository import Adw, GLib  # noqa: E402  # ty:ignore[unresolved-import]

if TYPE_CHECKING:
    from concurrent.futures import Future

    from bleak.backends.device import BLEDevice


class Recorder:
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
            [CyclingSample | HeartRateSample | RunningSample | TrainerSample], None
        ]
        | None = None,
        test_mode: bool = False,
    ):
        logger.debug(f"Initializing Recorder with sport_type {sport_type}")
        logger.debug(f"HR sensor: name={hr_name}, address={hr_address}")
        logger.debug(f"Speed sensor: name={speed_name}, address={speed_address}")
        logger.debug(f"Cadence sensor: name={cadence_name}, address={cadence_address}")
        logger.debug(f"Power sensor: name={power_name}, address={power_address}")
        logger.debug(
            f"Trainer sensor: name={trainer_name}, address={trainer_address}, machine_type={trainer_machine_type}"
        )

        self._ble_lock = asyncio.Lock()  # Lock for BLE operations
        self._thread: threading.Thread | None = None

        # Disable write when in test mode
        self.test_mode = bool(test_mode)

        self.weight_kg = weight_kg
        self.sport_type = sport_type
        self.on_sample = on_sample_update
        self.on_error = on_error
        self.db = DatabaseManager(database_url=database_url)
        self.stat_calc = StatsCalculator(self.db)
        self.loop = asyncio.new_event_loop()
        self._stop_event = asyncio.Event()
        self._recording = False
        self.activity_id = None
        self._start_ms = None
        self._pending_erg_watts: int | None = None
        self._erg_retry_task: Future | None = None
        self._erg_applied_watts: int | None = None

        # Sensors
        self.hr_name: str | None = hr_name
        self.hr_address: str | None = hr_address
        self.hr_device: BLEDevice | None = None
        self.speed_name: str | None = speed_name
        self.speed_address: str | None = speed_address
        self.speed_device: BLEDevice | None = None
        self.cadence_name: str | None = cadence_name
        self.cadence_address: str | None = cadence_address
        self.cadence_device: BLEDevice | None = None
        self.power_name: str | None = power_name
        self.power_address: str | None = power_address
        self.power_device: BLEDevice | None = None

        # Trainer (FTMS) configuration (separated by sport type upstream)
        self.trainer_name: str | None = trainer_name
        self.trainer_address: str | None = trainer_address
        self.trainer_machine_type: MachineType | None = trainer_machine_type
        self.trainer_device: BLEDevice | None = None

        # Rolling 3 bpm for smoothinng out hr readings
        self._bpm_history: deque[int] = deque(maxlen=3)

        # Connection status
        self.hr_connected = False
        self.speed_connected = False
        self.cadence_connected = False
        self.power_connected = False
        self.distance_connected = False

        # BLE muxes (only created if corresponding sensors are configured)
        self._speed_mux: RunningMux | CyclingMux | None = None
        self.trainer_mux: TrainerMux | None = None
        self._hr_mux: HeartRateMux | None = None

        self._dist0_m = None  # Fallback if sensor doesn't support reset

        # Current manually-set incline (percent), persisted into each metric row
        self.incline_percent: float | None = None
        self._current_altitude_m: float = 0.0
        self._last_distance_m: float | None = None

        # BLE Discover devices
        self.devices: list[BLEDevice] = []

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def shutdown(self):
        logger.debug("Recorder.shutdown called")
        if not self.loop.is_running():
            logger.debug("Loop not running, returning")
            return

        def _stop():
            self._stop_event.set()

        # Stop recording on shutdown
        self.stop_recording()

        self.loop.call_soon_threadsafe(_stop)
        if self._thread:
            self._thread.join(timeout=10)

    def start_recording(self):
        if not self._recording:
            # Only create an activity when not in test mode
            if not self.test_mode:
                self.activity_id = self.db.start_activity(sport_type=self.sport_type)
            else:
                self.activity_id = None
            self._recording = True
            self._start_ms = None
            self._dist0_m = None
            self._current_altitude_m = 0.0
            self._last_distance_m = None
            self._schedule_reset_distance()

    def stop_recording(self):
        if self._recording:
            if self.activity_id is not None:
                self.db.stop_activity(self.activity_id)
                self.stat_calc.compute_for_activity(self.activity_id)

            self._recording = False

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._workflow())

    # --- HR handling ---
    def _handle_hr_sample(self, sample: HeartRateSample) -> None:
        """Handle a HeartRateSample from HeartRateMux."""
        if sample.heart_rate_bpm is None:
            return

        logger.bind(data=sample).trace("Handling heart rate sample")

        # Initialize the session start
        if self._start_ms is None:
            self._start_ms = sample.timestamp_ms

        delta_ms = int(sample.timestamp_ms - self._start_ms)

        # Smooth out the bpm using a rolling median
        self._bpm_history.append(sample.heart_rate_bpm)
        smoothed_bpm = int(median(self._bpm_history))

        # Cleaned sample for UI
        cleaned_sample = HeartRateSample(
            timestamp_ms=delta_ms,
            heart_rate_bpm=smoothed_bpm,
        )
        GLib.idle_add(self.on_sample, cleaned_sample)

        logger.bind(data=cleaned_sample).trace("Processed heart rate sample")

        # Persist to the DB if recording
        if self._recording and self.activity_id:
            self.db.insert_heart_rate(
                self.activity_id,
                delta_ms,
                smoothed_bpm,
                sample.rr_interval_ms,
                sample.energy_expended_kcal,
            )

    # --- Running handling ---
    def _handle_running_sample(self, sample: RunningSample):
        if not self.on_sample:
            return

        logger.bind(data=sample).trace("Handling running sample")

        if self._start_ms is None:
            self._start_ms = sample.timestamp_ms

        delta_ms = int(sample.timestamp_ms - self._start_ms)

        watts = sample.power_watts
        adjusted_distance_m = sample.distance_m
        altitude_m = self._accumulate_altitude(sample.distance_m)

        if not self.trainer_mux and watts and self.weight_kg and self.incline_percent:
            # Estimate additional power from incline for footpods
            # using speed incline formula derived from QZ reference and Stryd calibration data:
            # vwatts = (a + b * speed_kmh) * incline
            # a = -0.96, b = 1.33
            # Reference: https://github.com/cagnulein/qdomyos-zwift/commit/c22f0568fd1db86cfdf07e749ea140f21df95e4b
            a = -0.96
            b = 1.33
            incline = self.incline_percent
            speed_kmh = sample.speed_kph or 0.0

            speed_term = (a + b * speed_kmh) * incline
            watts = round(watts + speed_term)

            additional_log = {
                "weight_kg": self.weight_kg,
                "incline_percent": self.incline_percent,
                "speed_kmh": speed_kmh,
                "speed_term": speed_term,
                "watts_before": sample.power_watts,
                "watts_after": watts,
            }
            logger.bind(data=additional_log).trace(
                "Estimating additional power from incline for footpod sample",
            )

            # Clamp to 0 watts
            watts = max(watts, 0)

        # Adjust distance by baseline if needed
        if self._dist0_m is None and sample.distance_m is not None:
            # If SC reset worked, first distance will be ~0; if not, this becomes our baseline.
            self._dist0_m = sample.distance_m

        if sample.distance_m is not None and self._dist0_m is not None:
            adjusted_distance_m = max(0.0, sample.distance_m - self._dist0_m)

        cleaned_sample = sample.model_copy(
            update={
                "timestamp_ms": delta_ms,
                "distance_m": adjusted_distance_m,
                "power_watts": watts,
                "altitude_m": altitude_m,
            },
        )

        GLib.idle_add(self.on_sample, cleaned_sample)

        logger.bind(data=cleaned_sample).trace("Processed running sample")

        # Persist to DB if recording
        if self._recording and self.activity_id:
            self.db.insert_running_metrics(
                self.activity_id,
                cleaned_sample,
                incline_percent=self.incline_percent,
            )

    def _handle_cycling_sample(self, sample: CyclingSample):
        if not self.on_sample:
            return

        logger.bind(data=sample).trace("Handling cycling sample")

        if self._start_ms is None:
            self._start_ms = sample.timestamp_ms

        delta_ms = int(sample.timestamp_ms - self._start_ms)

        adjusted_distance_m = sample.distance_m
        altitude_m = self._accumulate_altitude(sample.distance_m)

        # Adjust distance by baseline if needed
        if self._dist0_m is None and sample.distance_m is not None:
            # If SC reset worked, first distance will be ~0; if not, this becomes our baseline.
            self._dist0_m = sample.distance_m

        if sample.distance_m is not None and self._dist0_m is not None:
            adjusted_distance_m = max(0.0, sample.distance_m - self._dist0_m)

        cleaned_sample = sample.model_copy(
            update={
                "timestamp_ms": delta_ms,
                "distance_m": adjusted_distance_m,
                "altitude_m": altitude_m,
            },
        )

        GLib.idle_add(self.on_sample, cleaned_sample)

        logger.bind(data=cleaned_sample).trace("Processed cycling sample")

        # Persist to DB if recording
        if self._recording and self.activity_id:
            self.db.insert_cycling_metrics(
                self.activity_id,
                cleaned_sample,
                incline_percent=self.incline_percent,
            )

    def _handle_trainer_sample(self, sample: TrainerSample):
        if not self.on_sample:
            return

        logger.bind(data=sample).trace("Handling trainer sample")

        if self._start_ms is None:
            self._start_ms = sample.timestamp_ms

        delta_ms = int(sample.timestamp_ms - self._start_ms)
        adjusted_distance_m = sample.distance_m

        if (
            sample.target_power is not None
            and self._pending_erg_watts is None
            and self._erg_applied_watts != sample.target_power
        ):
            logger.debug(
                f"Trainer target power {sample.target_power} watts differs from applied {self._erg_applied_watts} watts, scheduling update"
            )
            self._pending_erg_watts = sample.target_power
            if not self.test_mode:
                self._ensure_erg_retry_loop()

        # Adjust distance by baseline if needed
        if self._dist0_m is None and sample.distance_m is not None:
            self._dist0_m = sample.distance_m

        if sample.distance_m is not None and self._dist0_m is not None:
            adjusted_distance_m = max(0.0, sample.distance_m - self._dist0_m)

        cleaned_sample = sample.model_copy(
            update={"timestamp_ms": delta_ms, "distance_m": adjusted_distance_m},
        )

        # Update UI
        GLib.idle_add(self.on_sample, cleaned_sample)

        logger.bind(data=cleaned_sample).trace("Processed trainer sample")

        # Persist to DB if recording
        if self._recording and self.activity_id:
            if self.sport_type == SportTypesEnum.biking:
                self.db.insert_cycling_metrics(
                    self.activity_id,
                    cleaned_sample,
                    incline_percent=self.incline_percent,
                )
            elif self.sport_type == SportTypesEnum.running:
                self.db.insert_running_metrics(
                    self.activity_id,
                    cleaned_sample,
                    incline_percent=self.incline_percent,
                )
            else:
                logger.error(f"Unknown sport type {self.sport_type} for trainer sample insertion")

    async def _workflow(self):
        logger.debug("Starting Recorder workflow")
        # Run both device loops concurrently (if configured)
        if self._stop_event._loop is not self.loop:
            self._stop_event = asyncio.Event()

        # Scan for BLE devices upfront, call bleaksport with found devices to speed up connection
        self.devices = await BleakScanner.discover(
            timeout=5.0,
        )

        logger.debug(f"BLE scan complete, found {len(self.devices)} devices")
        logger.bind(data=self.devices).trace("Discovered BLE devices")
        device_tasks = []

        logger.debug("Matching configured devices to scan results")
        for d in self.devices:
            # Heart Rate
            if (self.hr_address and d.address == self.hr_address) or (
                self.hr_name and d.name == self.hr_name
            ):
                logger.debug(f"Matched HR device from scan: {d.address} ({d.name})")
                self.hr_device = d

            # Speed
            if (self.speed_address and d.address == self.speed_address) or (
                self.speed_name and d.name == self.speed_name
            ):
                logger.debug(f"Matched speed device from scan: {d.address} ({d.name})")
                self.speed_device = d

            # Cadence
            if (self.cadence_address and d.address == self.cadence_address) or (
                self.cadence_name and d.name == self.cadence_name
            ):
                logger.debug(f"Matched cadence device from scan: {d.address} ({d.name})")
                self.cadence_device = d

            # Power
            if (self.power_address and d.address == self.power_address) or (
                self.power_name and d.name == self.power_name
            ):
                logger.debug(f"Matched power device from scan: {d.address} ({d.name})")
                self.power_device = d

            # Trainer
            if (self.trainer_address and d.address == self.trainer_address) or (
                self.trainer_name and d.name == self.trainer_name
            ):
                logger.debug(f"Matched trainer device from scan: {d.address} ({d.name})")
                self.trainer_device = d

        if not (
            self.hr_device
            or self.speed_device
            or self.cadence_device
            or self.power_device
            or self.trainer_device
        ):
            logger.debug(
                "No configured devices found in BLE scan results,"
                " starting loops without matched devices and wait for connections",
            )
        else:
            logger.debug(
                "Device matching complete, starting loops for found devices",
            )

        hr_started = False
        speed_started = False
        trainer_started = False
        # Start loops for found devices first
        if self.hr_device:
            logger.debug(
                f"Starting HR loop with device {self.hr_device.address} ({self.hr_device.name})",
            )
            hr_started = True
            device_tasks.append(self.loop.create_task(self._hr_loop()))
        if self.speed_device or self.cadence_device or self.power_device:
            logger.debug(
                "Starting speed loop with devices:"
                f" speed={self.speed_device.address if self.speed_device else 'none'} ({self.speed_device.name if self.speed_device else 'none'}), "
                f" cadence={self.cadence_device.address if self.cadence_device else 'none'} ({self.cadence_device.name if self.cadence_device else 'none'}), "
                f" power={self.power_device.address if self.power_device else 'none'} ({self.power_device.name if self.power_device else 'none'})"
            )
            speed_started = True
            device_tasks.append(self.loop.create_task(self._speed_loop()))
        if self.trainer_device:
            logger.debug(
                f"Starting trainer loop with device {self.trainer_device.address} ({self.trainer_device.name})"
            )
            trainer_started = True
            device_tasks.append(self.loop.create_task(self._trainer_loop()))

        # Start loops for any remaining configured devices that weren't found in the initial scan (they may still connect if they come online after the scan starts)
        if not hr_started and (self.hr_address or self.hr_name):
            logger.debug("Starting HR loop without matched device (will wait for connection)")
            device_tasks.append(self.loop.create_task(self._hr_loop()))

        if not speed_started and (
            self.speed_address
            or self.speed_name
            or self.cadence_address
            or self.cadence_name
            or self.power_address
            or self.power_name
        ):
            logger.debug("Starting speed loop without matched devices (will wait for connections)")
            device_tasks.append(self.loop.create_task(self._speed_loop()))

        if not trainer_started and (self.trainer_address or self.trainer_name):
            logger.debug("Starting trainer loop without matched device (will wait for connection)")
            device_tasks.append(self.loop.create_task(self._trainer_loop()))

        if not device_tasks:
            logger.warning("No device loops started — check configuration and BLE availability")
            return

        # Wait for explicit stop only — let each mux's internal loop handle reconnects
        await self._stop_event.wait()
        logger.debug(f"Stop event received, cancelling {len(device_tasks)} device tasks")

        for t in device_tasks:
            t.cancel()

        # Wait with a timeout so a stuck mux can't hang shutdown
        for t in device_tasks:
            try:
                await asyncio.wait_for(asyncio.shield(t), timeout=10.0)
                logger.debug(f"Task {t.get_name()} finished cleanly")
            except TimeoutError:
                logger.warning(f"Task {t.get_name()} did not finish within 10s after cancel")
            except asyncio.CancelledError:
                logger.debug(f"Task {t.get_name()} cancelled")
            except Exception as e:
                logger.warning(f"Task {t.get_name()} raised on shutdown: {e}")

        logger.debug("Workflow exiting")

    async def _speed_loop(self) -> None:
        if self.sport_type == SportTypesEnum.running:
            mux = RunningMux(
                speed_addr=self.speed_device or self.speed_address,
                cadence_addr=self.cadence_device or self.cadence_address,
                power_addr=self.power_device or self.power_address,
                on_sample=self._handle_running_sample,
                on_status=self._on_ble_error,
                on_link=self._on_running_link,
                ble_lock=self._ble_lock,
            )
        elif self.sport_type == SportTypesEnum.biking:
            csc_addr = (
                self.speed_device
                or self.cadence_device
                or self.speed_address
                or self.cadence_address
            )
            mux = CyclingMux(
                csc_addr=csc_addr,
                cps_addr=self.power_device or self.power_address,
                on_sample=self._handle_cycling_sample,
                on_status=self._on_ble_error,
                on_link=self._on_running_link,  # same link handler for cycling mux
                ble_lock=self._ble_lock,
            )
        else:
            logger.error(f"Unknown sport type {self.sport_type} for speed loop")
            return
        self._speed_mux = mux
        try:
            await mux.start()
        finally:
            logger.debug("Cleaning up speed connections")
            await mux.stop()
            self._speed_mux = None

    def _on_running_link(self, _addr: str, connected: bool, roles: dict[str, bool]) -> None:
        # RSCS drives both speed & cadence cards
        self.speed_connected = connected and roles.get("rsc", False)
        self.cadence_connected = connected and roles.get("rsc", False)
        self.distance_connected = connected and roles.get("rsc", False)
        self.power_connected = connected and roles.get("cps", False)

    async def _trainer_loop(self) -> None:
        self.trainer_mux = TrainerMux(
            addr=self.trainer_device or self.trainer_address,
            machine_type=self.trainer_machine_type,
            on_sample=self._handle_trainer_sample,
            on_status=self._on_ble_error,
            on_link=self._on_trainer_link,
        )
        try:
            await self.trainer_mux.start()
        finally:
            logger.debug("Cleaning up trainer connections")
            with contextlib.suppress(Exception):
                await self.trainer_mux.stop()
            self.trainer_mux = None

    def _on_trainer_link(self, _addr: str, connected: bool, _info: dict[str, bool]) -> None:
        self.speed_connected = connected
        self.cadence_connected = connected
        self.power_connected = connected
        self.distance_connected = connected

        if not connected:
            # reset erg watts on disconnect so it applies immediately on reconnect
            self._erg_applied_watts = None

    async def _hr_loop(self) -> None:
        """Connect to the HR monitor via HeartRateMux and stream samples."""
        self._hr_mux = HeartRateMux(
            addr=self.hr_device or self.hr_address,
            name=self.hr_name,
            on_sample=self._handle_hr_sample,
            on_status=self._on_ble_error,
            on_link=self._on_hr_link,
            ble_lock=self._ble_lock,
        )
        try:
            await self._hr_mux.start()
        finally:
            logger.debug("Cleaning up hr connections")
            with contextlib.suppress(Exception):
                await self._hr_mux.stop()
            self._hr_mux = None

    def _on_hr_link(self, addr: str, connected: bool, roles: dict[str, bool]) -> None:
        self.hr_connected = connected

    def _on_ble_error(self, msg: str) -> None:
        GLib.idle_add(lambda: self.on_error(msg))

    def _schedule_reset_distance(self) -> None:
        """Kick an async reset in the BLE loop without blocking the UI."""
        if not self.loop.is_running():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._reset_distance_workflow(), self.loop)
        except Exception as e:
            self._on_ble_error(f"Failed to schedule distance reset: {e}")

    def set_target_power(self, watts: int) -> None:
        """Set target power on the trainer if supported."""
        logger.debug(f"Trying to set target power to {watts} watts")

        watts = int(watts)
        # Store intent
        self._pending_erg_watts = watts

        # Reset applied marker so retry loop knows this needs applying
        if self._erg_applied_watts != watts:
            self._erg_applied_watts = None

        if not self.test_mode:
            self._ensure_erg_retry_loop()

    def _ensure_erg_retry_loop(self) -> None:
        if self._erg_retry_task and not self._erg_retry_task.done():
            return  # already running

        self._erg_retry_task = asyncio.run_coroutine_threadsafe(
            self._erg_retry_loop(),
            self.loop,
        )

    async def _erg_retry_loop(self) -> None:
        while True:
            # Read the current pending target at the start of each iteration.
            target = self._pending_erg_watts
            if target is None:
                # Nothing pending anymore.
                return

            mux = self.trainer_mux

            if mux and mux.is_connected:
                try:
                    result = await mux.set_target_power(target)
                    # Only clear the pending value if it wasn't updated
                    # while the await was in-flight.
                    if self._pending_erg_watts == result:
                        self._pending_erg_watts = None
                        self._erg_applied_watts = result

                        return

                except Exception as e:
                    self._on_ble_error(f"ERG set failed, retrying: {e}")

            await asyncio.sleep(2.0)  # retry interval

    async def _reset_distance_workflow(self, *, wait_s: float = 6.0) -> None:
        """
        Wait up to wait_s for RSCS to be connected, then try SC Control Point reset.
        Fall back silently (baseline subtraction will handle it).
        """
        # Wait a little for the RSCS link to come up
        t0 = self.loop.time()
        while (self.loop.time() - t0) < wait_s and not self._stop_event.is_set():
            if self.speed_connected and self._speed_mux:
                break
            await asyncio.sleep(0.2)

        mux = self._speed_mux
        if not mux:
            return

        try:
            ok = await mux.reset_distance()
            if ok:
                # Optional: set baseline to 0 so first sample shows exactly 0.00 mi.
                self._dist0_m = 0.0
            else:
                # Not supported / timed out — baseline logic will take over
                print("Sensor didn't accept distance reset; using baseline")
        except Exception as e:
            # Don’t fail the session; just fall back
            print(f"SC Control Point reset failed: {e}")

    def set_incline(self, percent: float | None) -> None:
        """Set the current incline percentage (None = flat / unknown)."""
        self.incline_percent = percent

    def _accumulate_altitude(self, dist_m: float | None) -> float:
        """Update and return current altitude based on distance delta and current incline."""
        if dist_m is None or self.incline_percent is None:
            return self._current_altitude_m

        if self._last_distance_m is not None:
            delta = max(0.0, dist_m - self._last_distance_m)
            self._current_altitude_m += delta * (self.incline_percent / 100.0)

        self._last_distance_m = dist_m
        return self._current_altitude_m

    # --- Test-mode injection ---
    def inject_test_sample(
        self,
        sample: CyclingSample | RunningSample | TrainerSample | HeartRateSample,
    ) -> None:
        """
        Directly inject a pre-built sample into the recorder,
        bypassing BLE. Used exclusively in test_mode to exercise the full recorder pipeline
        (distance baseline, altitude accumulation, DB writes, UI callbacks) from
        simulated data produced by the UI layer.

        Safe to call from the GTK main thread — the handlers only touch recorder state
        and schedule GLib.idle_add callbacks; no asyncio involvement is needed.
        """
        if not self.test_mode:
            logger.warning("inject_test_sample called outside of test_mode — ignoring")
            return

        if isinstance(sample, TrainerSample):
            self._handle_trainer_sample(sample)
        elif isinstance(sample, RunningSample):
            self._handle_running_sample(sample)
        elif isinstance(sample, CyclingSample):
            self._handle_cycling_sample(sample)
        elif isinstance(sample, HeartRateSample):
            self._handle_hr_sample(sample)
        else:
            logger.error(f"inject_test_sample: unrecognised sample type {type(sample)}")

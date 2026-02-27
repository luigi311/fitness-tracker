import asyncio
import contextlib
import re
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from statistics import median

import gi
from bleak import BleakError, BleakScanner
from bleaksport import MachineType
from bleaksport.running import RunningMux, RunningSample
from bleaksport.trainer import TrainerMux, TrainerSample
from loguru import logger

from fitness_tracker.database import DatabaseManager
from fitness_tracker.hr_provider import HEART_RATE_SERVICE_UUID, connect_and_stream

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Adw, GLib


INPROGRESS_RE = re.compile(r"InProgress", re.IGNORECASE)


class Recorder:
    def __init__(
        self,
        on_bpm_update: Callable[[float, int], None],
        database_url: str,
        profile: str,
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
        on_sample_update: Callable[[float, float, int, float | None, float | None], None]
        | None = None,
        test_mode: bool = False,
    ):
        logger.debug(f"Initializing Recorder with profile {profile}")
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

        self.on_bpm = on_bpm_update
        self.on_sample = on_sample_update
        self.on_error = on_error
        self.db = DatabaseManager(database_url=database_url)
        self.loop = asyncio.new_event_loop()
        self.queue: asyncio.Queue = asyncio.Queue()
        self._stop_event = asyncio.Event()
        self._recording = False
        self._activity_id = None
        self._start_ms = None
        self._pending_erg_watts: int | None = None
        self._erg_retry_task: Future | None = None
        self._erg_applied_watts: int | None = None

        # Profile metadata (used by UI to avoid rebuilding unnecessarily)
        self.profile = profile.strip().lower()

        # Sensors
        self.hr_name = hr_name
        self.hr_address = hr_address
        self.speed_name = speed_name
        self.speed_address = speed_address
        self.cadence_name = cadence_name
        self.cadence_address = cadence_address
        self.power_name = power_name
        self.power_address = power_address

        # Trainer (FTMS) configuration (separated by profile upstream)
        self.trainer_name = trainer_name
        self.trainer_address = trainer_address
        self.trainer_machine_type = trainer_machine_type

        # Rolling 3 bpm for smoothinng out hr readings
        self._bpm_history: deque[int] = deque(maxlen=3)

        # Connection status
        self.hr_connected = False
        self.speed_connected = False
        self.cadence_connected = False
        self.power_connected = False
        self.distance_connected = False

        # Clearing total distance on new recording
        self._running_mux: RunningMux | None = None
        self.trainer_mux: TrainerMux | None = None
        self._dist0_m = None  # Fallback if sensor doesn't support reset

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def shutdown(self):
        if not self.loop.is_running():
            return

        def _stop():
            self._stop_event.set()

        # Stop recording on shutdown
        self.stop_recording()

        self.loop.call_soon_threadsafe(_stop)
        if self._thread:
            self._thread.join(timeout=3)

    def start_recording(self):
        if not self._recording:
            # Only create an activity when not in test mode
            if not self.test_mode:
                self._activity_id = self.db.start_activity()
            else:
                self._activity_id = None
            self._recording = True
            self._start_ms = None
            self._dist0_m = None
            self._schedule_reset_distance()

    def stop_recording(self):
        if self._recording:
            if self._activity_id is not None:
                self.db.stop_activity(self._activity_id)
            self._recording = False

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._workflow())

    # --- HR handling ---
    def _handle_hr_sample(self, t_ms: int, bpm: int, rr: float | None, energy: float | None):
        # initialize the session start
        if self._start_ms is None:
            self._start_ms = t_ms

        # Elapsed ms
        delta_ms = t_ms - self._start_ms

        # Smooth out the bpm using a rolling median
        self._bpm_history.append(bpm)
        smoothed_bpm = int(median(self._bpm_history))

        # Update the live UI
        GLib.idle_add(self.on_bpm, delta_ms, smoothed_bpm)

        # Persist to the DB if recording
        if self._recording and self._activity_id:
            self.db.insert_heart_rate(self._activity_id, delta_ms, smoothed_bpm, rr, energy)

    # --- Running handling ---
    def _handle_running_sample(self, sample: RunningSample):
        if not self.on_sample:
            return

        t_ms = int(sample.timestamp * 1000.0)
        if self._start_ms is None:
            self._start_ms = t_ms
        delta_ms = t_ms - self._start_ms

        speed_mps = float(sample.speed_mps or 0.0)
        cadence = int(sample.cadence_spm or 0)
        dist_m = sample.total_distance_m  # may be None
        watts = float(sample.power_watts) if sample.power_watts is not None else None

        # Adjust distance by baseline if needed
        if self._recording:
            if self._dist0_m is None and dist_m is not None:
                # If SC reset worked, first distance will be ~0; if not, this becomes our baseline.
                self._dist0_m = float(dist_m)
            if dist_m is not None and self._dist0_m is not None:
                dist_m = max(0.0, float(dist_m) - self._dist0_m)

        # Update UI
        GLib.idle_add(self.on_sample, delta_ms, speed_mps, cadence, dist_m, watts)

        # Persist to DB if recording
        if self._recording and self._activity_id:
            self.db.insert_running_metrics(
                self._activity_id,
                delta_ms,
                speed_mps=float(speed_mps),
                cadence_spm=int(cadence),
                stride_length_m=(
                    float(sample.stride_length_m) if sample.stride_length_m is not None else None
                ),
                total_distance_m=(float(dist_m) if dist_m is not None else None),
                power_watts=(float(watts) if watts is not None else None),
            )

    def _handle_trainer_sample(self, sample: TrainerSample):
        if not self.on_sample:
            return

        logger.trace(f"Handling trainer sample: {sample}")

        t_ms = int(sample.timestamp * 1000.0)
        if self._start_ms is None:
            self._start_ms = t_ms
        delta_ms = t_ms - self._start_ms

        speed_mps = float(sample.speed_mps or 0.0)
        cadence = int(sample.cadence_rpm or 0)
        dist_m = sample.distance_m
        watts = float(sample.power_watts) if sample.power_watts is not None else None

        # Adjust distance by baseline if needed
        if self._recording:
            if self._dist0_m is None and dist_m is not None:
                self._dist0_m = float(dist_m)
            if dist_m is not None and self._dist0_m is not None:
                dist_m = max(0.0, float(dist_m) - self._dist0_m)

        # Update UI
        GLib.idle_add(self.on_sample, delta_ms, speed_mps, cadence, dist_m, watts)

        # Persist to DB if recording
        if self._recording and self._activity_id:
            if sample.machine_type == MachineType.INDOOR_BIKE:
                logger.trace(
                    f"Inserting cycling metrics into DB: \n\tActivity ID: {self._activity_id} \n\tDelta MS: {delta_ms} \n\tSpeed: {speed_mps} \n\tCadence: {cadence} \n\tDistance: {dist_m} \n\tPower: {watts}"
                )
                self.db.insert_cycling_metrics(
                    self._activity_id,
                    delta_ms,
                    speed_mps=float(speed_mps),
                    cadence_rpm=int(cadence),
                    total_distance_m=(float(dist_m) if dist_m is not None else None),
                    power_watts=(float(watts) if watts is not None else None),
                )
            elif sample.machine_type == MachineType.TREADMILL:
                logger.trace(
                    f"Inserting running metrics into DB: \n\tActivity ID: {self._activity_id} \n\tDelta MS: {delta_ms} \n\tSpeed: {speed_mps} \n\tCadence: {cadence} \n\tDistance: {dist_m} \n\tPower: {watts}"
                )
                self.db.insert_running_metrics(
                    self._activity_id,
                    delta_ms,
                    speed_mps=float(speed_mps),
                    cadence_spm=int(cadence),
                    stride_length_m=None,
                    total_distance_m=(float(dist_m) if dist_m is not None else None),
                    power_watts=(float(watts) if watts is not None else None),
                )
            else:
                logger.error(f"Unknown machine type {sample.machine_type} for trainer sample")

    async def _workflow(self):
        logger.debug("Starting Recorder workflow")
        # Run both device loops concurrently (if configured)
        if self._stop_event._loop is not self.loop:
            self._stop_event = asyncio.Event()

        device_tasks = [asyncio.create_task(self._hr_loop())]

        logger.debug(f"on_sample callback provided: {self.on_sample is not None}")

        have_any_sensors = any([self.speed_address, self.cadence_address, self.power_address])
        logger.debug(f"Sensors configured: {have_any_sensors}")
        if have_any_sensors and self.on_sample:
            device_tasks.append(asyncio.create_task(self._running_loop()))

        have_trainer = bool(self.trainer_address)
        logger.debug(f"Trainer configured: {have_trainer}")
        if have_trainer and self.on_sample:
            device_tasks.append(asyncio.create_task(self._trainer_loop()))

        stop_task = asyncio.create_task(self._stop_event.wait())

        # Wait until either a device task finishes or we were asked to stop
        done, pending = await asyncio.wait(
            device_tasks + [stop_task], return_when=asyncio.FIRST_COMPLETED
        )

        # If we were asked to stop, cancel device tasks
        for t in device_tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

    async def _hr_loop(self) -> None:
        target = None
        while not self._stop_event.is_set():
            try:
                # Resolve address ONCE (if not provided)
                if not self.hr_address and self.hr_name:
                    async with self._ble_lock:
                        devices = await BleakScanner.discover(
                            timeout=5.0, service_uuids=[HEART_RATE_SERVICE_UUID]
                        )
                    cand = next((d for d in devices if d.name == self.hr_name), None)
                    if cand:
                        self.hr_address = cand.address

                # Find device by address (no general scan)
                if not self.hr_address:
                    await asyncio.sleep(3.0)
                    continue

                async with self._ble_lock:
                    target = await BleakScanner.find_device_by_address(self.hr_address, timeout=5.0)
                if not target:
                    if self.hr_connected:
                        self.hr_connected = False

                    # device not seen right now; back off but don’t scan wildly
                    await asyncio.sleep(3.0)
                    continue

                # Connect + notifications (serialized)
                try:
                    async with self._ble_lock:
                        async for t_ms, bpm, rr, energy in connect_and_stream(
                            target, self.queue, self._on_ble_error
                        ):
                            if not self.hr_connected:
                                self.hr_connected = True
                            self._handle_hr_sample(t_ms, bpm, rr, energy)
                except BleakError as e:
                    self.hr_connected = False
                    if INPROGRESS_RE.search(str(e)):
                        await asyncio.sleep(1.5)
                    else:
                        self._on_ble_error(f"🔄  HR BLE error, will retry: {e}")

            except asyncio.CancelledError:
                # task is being cancelled during shutdown; exit quietly
                return
            except Exception as e:
                self.hr_connected = False
                # If InProgress,
                if INPROGRESS_RE.search(str(e)):
                    # This can happen if the device is in a weird state; just wait and retry
                    logger.warning("HR loop ble connection in progress, will retry")
                    await asyncio.sleep(1.5)
                else:
                    # include the type so empty messages aren’t mysterious
                    self._on_ble_error(f"HR loop unexpected error: {type(e).__name__}: {e!s}")

            await asyncio.sleep(2.0)

    async def _running_loop(self) -> None:
        mux = RunningMux(
            speed_addr=self.speed_address or self.cadence_address,
            cadence_addr=self.cadence_address,
            power_addr=self.power_address,
            on_sample=self._handle_running_sample,
            on_status=self._on_ble_error,
            on_link=self._on_running_link,
        )
        self._running_mux = mux
        try:
            await mux.start()
        finally:
            with contextlib.suppress(Exception):
                await mux.stop()
            self._running_mux = None

    def _on_running_link(self, _addr: str, connected: bool, roles: dict[str, bool]) -> None:
        # RSCS drives both speed & cadence cards
        self.speed_connected = connected and roles.get("rsc", False)
        self.cadence_connected = connected and roles.get("rsc", False)
        self.distance_connected = connected and roles.get("rsc", False)
        self.power_connected = connected and roles.get("cps", False)

    def _on_trainer_link(self, _addr: str, connected: bool, _info: dict[str, bool]) -> None:
        self.speed_connected = connected
        self.cadence_connected = connected
        self.power_connected = connected
        self.distance_connected = connected

        if not connected:
            # reset erg watts on disconnect so it applies immediately on reconnect
            self._erg_applied_watts = None

    async def _trainer_loop(self) -> None:
        logger.debug(
            f"Starting trainer loop with address {self.trainer_address} and machine type {self.trainer_machine_type}"
        )
        self.trainer_mux = TrainerMux(
            addr=self.trainer_address,
            machine_type=self.trainer_machine_type,
            on_sample=self._handle_trainer_sample,
            on_status=self._on_ble_error,
            on_link=self._on_trainer_link,
            sticky_ttl_s=3.0,
        )
        try:
            await self.trainer_mux.start()
        finally:
            with contextlib.suppress(Exception):
                await self.trainer_mux.stop()
            self.trainer_mux = None

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
                    await mux.set_target_power(target)
                    # Only clear the pending value if it wasn't updated
                    # while the await was in-flight.
                    if self._pending_erg_watts == target:
                        self._pending_erg_watts = None
                        self._erg_applied_watts = target
                        return

                except Exception as e:
                    self._on_ble_error(f"ERG set failed, retrying: {e}")

            await asyncio.sleep(2.0)  # retry interval

    def clear_target_power(self) -> None:
        """Clear any pending target power (e.g. when stopping a workout)."""
        self._pending_erg_watts = None

    async def _reset_distance_workflow(self, *, wait_s: float = 6.0) -> None:
        """
        Wait up to wait_s for RSCS to be connected, then try SC Control Point reset.
        Fall back silently (baseline subtraction will handle it).
        """
        # Wait a little for the RSCS link to come up
        t0 = self.loop.time()
        while (self.loop.time() - t0) < wait_s and not self._stop_event.is_set():
            if self.speed_connected and self._running_mux:
                break
            await asyncio.sleep(0.2)

        mux = self._running_mux
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

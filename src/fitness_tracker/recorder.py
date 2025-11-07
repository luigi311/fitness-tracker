import asyncio
import contextlib
import re
import threading
from collections import deque
from collections.abc import Callable
from statistics import median

import gi
from bleak import BleakError, BleakScanner
from bleaksport.running import RunningMux, RunningSample

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
        hr_name: str | None,
        hr_address: str | None,
        speed_name: str | None,
        speed_address: str | None,
        cadence_name: str | None,
        cadence_address: str | None,
        power_name: str | None,
        power_address: str | None,
        on_error: Callable[[str], None],
        *,
        on_running_update: Callable[[float, float, int, float | None, float | None], None]
        | None = None,
        test_mode: bool = False,
    ):
        self._ble_lock = asyncio.Lock()  # Lock for BLE operations
        self._thread: threading.Thread | None = None

        # Disable write when in test mode
        self.test_mode = bool(test_mode)

        self.on_bpm = on_bpm_update
        self.on_running = on_running_update
        self.on_error = on_error
        self.db = DatabaseManager(database_url=database_url)
        self.loop = asyncio.new_event_loop()
        self.queue: asyncio.Queue = asyncio.Queue()
        self._stop_event = asyncio.Event()
        self._recording = False
        self._activity_id = None
        self._start_ms = None

        # Sensors
        self.hr_name = hr_name
        self.hr_address = hr_address
        self.speed_name = speed_name
        self.speed_address = speed_address
        self.cadence_name = cadence_name
        self.cadence_address = cadence_address
        self.power_name = power_name
        self.power_address = power_address

        # Rolling 3 bpm for smoothinng out hr readings
        self._bpm_history: deque[int] = deque(maxlen=3)

        # Connection status
        self.hr_connected = False
        self.speed_connected = False
        self.cadence_connected = False
        self.power_connected = False

        # Clearing total distance on new recording
        self._running_mux: RunningMux | None = None
        self._dist0_m = None             # Fallback if sensor doesn't support reset

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
        if not self.on_running:
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
        GLib.idle_add(self.on_running, delta_ms, speed_mps, cadence, dist_m, watts)

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

    async def _workflow(self):
        # Run both device loops concurrently (if configured)
        if self._stop_event._loop is not self.loop:
            self._stop_event = asyncio.Event()

        device_tasks = [asyncio.create_task(self._hr_loop())]

        have_any_running = any(
            [self.speed_address, self.cadence_address, self.power_address]
        )
        if have_any_running and self.on_running:
            device_tasks.append(asyncio.create_task(self._running_loop()))

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
                    target = await BleakScanner.find_device_by_address(
                        self.hr_address, timeout=5.0
                    )
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

    def _on_running_link(self, _addr: str, connected: bool, rsc_ok: bool, cps_ok: bool) -> None:
        # RSCS drives both speed & cadence cards
        self.speed_connected = connected and rsc_ok
        self.cadence_connected = connected and rsc_ok
        self.power_connected = connected and cps_ok

    def _on_ble_error(self, msg: str) -> None:
        GLib.idle_add(lambda: self.on_error(msg))

    def _schedule_reset_distance(self) -> None:
        """Kick an async reset in the BLE loop without blocking the UI."""
        # Check if loop is running before scheduling
        if not self.loop.is_running():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._reset_distance_workflow(), self.loop)
        except Exception as e:
            self._on_ble_error(f"Failed to schedule distance reset: {e}")

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

        # Capture the mux value immediately after the wait loop to avoid race conditions
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
                self._on_ble_error("Sensor didn't accept distance reset; using baseline")
        except Exception as e:
            # Don’t fail the session; just fall back
            self._on_ble_error(f"SC Control Point reset failed: {e}")

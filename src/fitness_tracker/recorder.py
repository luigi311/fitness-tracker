import asyncio
import contextlib
import re
import threading
from collections import deque
from collections.abc import Callable
from statistics import median

import gi
from bleak import BleakClient, BleakError, BleakScanner
from bleaksport.running import RunningSample, RunningSession

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
        device_name: str,
        on_error: Callable[[str], None],
        device_address: str | None = None,
        *,
        on_running_update: Callable[[float, float, int, float | None, float | None], None]
        | None = None,
        running_device_name: str = "",
        running_device_address: str | None = None,
    ):
        self._ble_lock = asyncio.Lock()  # Lock for BLE operations
        self._thread: threading.Thread | None = None

        # Cache resolved addresses so we don’t keep scanning
        self._hr_addr_cache: str | None = device_address
        self._run_addr_cache: str | None = running_device_address

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

        # HR device
        self.device_name = device_name
        self.device_address = device_address

        # Running device
        self.running_device_name = running_device_name
        self.running_device_address = running_device_address

        # Rolling 3 bpm for smoothinng out hr readings
        self._bpm_history: deque[int] = deque(maxlen=3)

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def shutdown(self):
        if not self.loop.is_running():
            return

        def _stop():
            self._stop_event.set()

        self.loop.call_soon_threadsafe(_stop)
        if self._thread:
            self._thread.join(timeout=3)

    def start_recording(self):
        if not self._recording:
            self._activity_id = self.db.start_activity()
            self._recording = True
            self._start_ms = None

    def stop_recording(self):
        if self._recording:
            self.db.stop_activity(self._activity_id)
            self._recording = False

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._workflow())

    # --- HR handling ---
    def _handle_sample(self, t_ms: int, bpm: int, rr: float | None, energy: float | None):
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
        if self._recording:
            self.db.insert_heart_rate(self._activity_id, delta_ms, smoothed_bpm, rr, energy)

    # --- Running handling ---
    def _handle_running_sample(self, sample: RunningSample):
        if not self.on_running:
            return
        t_ms = int(sample.timestamp * 1000.0)
        if self._start_ms is None:
            self._start_ms = t_ms
        delta_ms = t_ms - self._start_ms

        speed_mps = sample.speed_mps
        cadence = int(sample.cadence_spm)
        dist_m = sample.total_distance_m  # may be None
        watts = float(sample.power_watts) if sample.power_watts is not None else None

        # Update UI
        GLib.idle_add(self.on_running, delta_ms, speed_mps, cadence, dist_m, watts)

        # Persist to DB if recording
        if self._recording:
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
        tasks = [asyncio.create_task(self._hr_loop())]
        if self.running_device_name or self.running_device_address:
            tasks.append(asyncio.create_task(self._running_loop()))
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

    async def _hr_loop(self) -> None:
        target = None
        while not self._stop_event.is_set():
            try:
                # Resolve address ONCE (if not provided)
                if not self._hr_addr_cache and self.device_name:
                    async with self._ble_lock:
                        devices = await BleakScanner.discover(
                            timeout=5.0, service_uuids=[HEART_RATE_SERVICE_UUID]
                        )
                    cand = next((d for d in devices if d.name == self.device_name), None)
                    if cand:
                        self._hr_addr_cache = cand.address

                if not self._hr_addr_cache and not self.device_address:
                    # nothing to do yet
                    await asyncio.sleep(3.0)
                    continue

                # Find device by address (no general scan)
                addr = self._hr_addr_cache or self.device_address
                async with self._ble_lock:
                    target = await BleakScanner.find_device_by_address(addr, timeout=5.0)

                if not target:
                    # device not seen right now; back off but don’t scan wildly
                    await asyncio.sleep(3.0)
                    continue

                # Connect + notifications (serialized)
                try:
                    async with self._ble_lock:
                        async for t_ms, bpm, rr, energy in connect_and_stream(
                            target, self.queue, self._on_ble_error
                        ):
                            self._handle_sample(t_ms, bpm, rr, energy)
                except BleakError as e:
                    if INPROGRESS_RE.search(str(e)):
                        await asyncio.sleep(1.5)  # brief backoff then retry
                    else:
                        self._on_ble_error(f"🔄  HR BLE error, will retry: {e}")

            except Exception as e:
                self._on_ble_error(f"HR loop unexpected error: {e}")

            await asyncio.sleep(2.0)

    async def _running_loop(self) -> None:
        session = RunningSession()
        session.on_running(self._handle_running_sample)

        bleak_client = None
        target = None

        while not self._stop_event.is_set():
            try:
                # Resolve address once if only a name was provided
                if not self._run_addr_cache and self.running_device_name:
                    async with self._ble_lock:
                        dev = await BleakScanner.find_device_by_filter(
                            lambda d, _: bool(d.name)
                            and self.running_device_name.lower() in d.name.lower()
                        )
                    if dev:
                        self._run_addr_cache = dev.address

                # If we still don't have an address, idle and retry
                if not (self._run_addr_cache or self.running_device_address):
                    await asyncio.sleep(3.0)
                    continue

                addr = self._run_addr_cache or self.running_device_address

                # Try to find current device presence
                async with self._ble_lock:
                    if addr:
                        target = await BleakScanner.find_device_by_address(addr, timeout=5.0)

                if not target:
                    await asyncio.sleep(3.0)
                    continue

                # Connect and subscribe (session handles start/stop of notifications)
                def _running_disconnect(_client) -> None:
                    self._on_ble_error("⚠️  Running device disconnected")

                try:
                    async with self._ble_lock:
                        if addr:
                            bleak_client = BleakClient(
                                addr, disconnected_callback=_running_disconnect
                            )
                            await bleak_client.connect()
                            await session.start(bleak_client)
                except BleakError as e:
                    if INPROGRESS_RE.search(str(e)):
                        await asyncio.sleep(1.5)
                        continue

                    self._on_ble_error(f"🔄  Running BLE error, will retry: {e}")
                    await asyncio.sleep(2.0)
                    continue

                # Stay connected until it drops
                while bleak_client and bleak_client.is_connected:
                    await asyncio.sleep(1.0)

            except BleakError as e:
                if INPROGRESS_RE.search(str(e)):
                    await asyncio.sleep(1.5)
                else:
                    self._on_ble_error(f"Running loop error: {e}")
            finally:
                try:
                    async with self._ble_lock:
                        if bleak_client:
                            with contextlib.suppress(Exception):
                                await session.stop(bleak_client)
                            with contextlib.suppress(Exception):
                                await bleak_client.disconnect()
                except Exception as e:
                    self._on_ble_error(f"Running cleanup error: {e}")

            await asyncio.sleep(2.0)

    def _on_ble_error(self, msg: str) -> None:
        GLib.idle_add(lambda: self.on_error(msg))

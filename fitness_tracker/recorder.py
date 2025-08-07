import asyncio
import threading
from collections import deque
from statistics import median
from typing import Callable

import gi
from bleak import BleakError, BleakScanner

from fitness_tracker.database import DatabaseManager
from fitness_tracker.hr_provider import HEART_RATE_SERVICE_UUID, connect_and_stream

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Adw, GLib


class Recorder:
    def __init__(
        self,
        on_bpm_update: Callable[[float, int], None],
        database_url: str,
        device_name: str,
        on_error: Callable[[str], None],
        device_address: str | None = None,
    ):
        self.on_bpm = on_bpm_update
        self.on_error = on_error
        self.db = DatabaseManager(database_url=database_url)
        self.loop = asyncio.new_event_loop()
        self.queue: asyncio.Queue = asyncio.Queue()
        self._recording = False
        self._activity_id = None
        self._start_ms = None
        self.device_name = device_name
        self.device_address = device_address

        # Rolling 3 bpm for smoothinng out hr readings
        self._bpm_history: deque[int] = deque(maxlen=3)

    def start(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

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
            self.db.insert_heart_rate(
                self._activity_id, delta_ms, smoothed_bpm, rr, energy
            )

    async def _workflow(self):
        target = None

        while True:
            # If we have a device address, try to connect directly
            if self.device_address:
                print(f"🔗  Trying address {self.device_address}…")
                try:
                    target = await BleakScanner.find_device_by_address(
                        self.device_address, timeout=5.0
                    )
                except Exception:
                    target = None

            # Fallback to name‐based discovery
            if not target:
                print("🔍  Discovering by name…")
                devices = await BleakScanner.discover(timeout=5.0)
                target = next((d for d in devices if d.name == self.device_name), None)

            if not target:
                print("❌  No device found (address or name)")
                GLib.idle_add(self.on_bpm, 0.0, -1)
                await asyncio.sleep(5.0)
                continue

            print(f"✅  Found device: {getattr(target, 'name', target)} ({target.address})")

            # Connect and stream
            try:
                async for t_ms, bpm, rr, energy in connect_and_stream(
                    target, self.queue, self._on_ble_error
                ):
                    self._handle_sample(t_ms, bpm, rr, energy)
            except BleakError as e:
                self._on_ble_error(f"🔄  BLE error, will retry: {e}")

            # pause before retry
            await asyncio.sleep(5.0)

    def _on_ble_error(self, msg: str):
        GLib.idle_add(lambda: self.on_error(msg))

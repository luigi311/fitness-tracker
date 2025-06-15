import asyncio
import threading
from typing import Callable
from fitness_tracker.database import DatabaseManager
from fitness_tracker.hr_provider import AVAILABLE_PROVIDERS
from bleak import BleakError, BleakScanner

import gi
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
        # 1) initialize the session start
        if self._start_ms is None:
            self._start_ms = t_ms

        # 2) compute elapsed seconds
        delta_ms = t_ms - self._start_ms
        t_sec = delta_ms / 1_000.0

        # 3) always update the live UI
        GLib.idle_add(self.on_bpm, t_sec, bpm)

        # 4) if we’re recording, persist to the DB
        if self._recording:
            self.db.insert_heart_rate(self._activity_id, t_ms, bpm, rr, energy)

    async def _workflow(self):
         # 1) Try direct connect by address (fastest / exact), if provided
        target = None

        while True:
            if self.device_address:
                print(f"🔗  Trying address {self.device_address}…")
                try:
                    target = await BleakScanner.find_device_by_address(
                        self.device_address, timeout=5.0
                    )
                except Exception:
                    target = None

            # 2) Fallback to name‐based discovery
            if not target:
                print("🔍  Discovering by name…")
                devices = await BleakScanner.discover(timeout=5.0)
                target = next((d for d in devices if d.name == self.device_name), None)

            if not target:
                print("❌  No device found (address or name)")
                GLib.idle_add(self.on_bpm, 0.0, -1)
                await asyncio.sleep(5.0)
                continue
            
            print(f"✅  Found device: {target.name} ({target.address})")
            provider_cls = next(
                (p for p in AVAILABLE_PROVIDERS if p.matches(target.name)), None
            )
            if not provider_cls:
                GLib.idle_add(self.on_bpm, 0.0, -1)
                return

        
            try:
                async for t_ms, bpm, rr, energy in provider_cls.stream(
                    target, self._on_ble_error
                ):
                    self._handle_sample(t_ms, bpm, rr, energy)
            except BleakError as e:
                self._on_ble_error(f"🔄  BLE error, will retry: {e}")

            # pause a bit before trying again
            await asyncio.sleep(5.0)


    def _on_ble_error(self, msg: str):
        # forward into the UI thread via the callback:
        GLib.idle_add(lambda: self.on_error(msg))
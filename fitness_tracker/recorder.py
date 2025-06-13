import asyncio
import threading
from .ble import scan_polar, connect_and_stream
from .database import DatabaseManager, Activity, HeartRate
from typing import Callable
from gi.repository import GLib


class Recorder:
    def __init__(
        self,
        on_bpm_update: Callable[[float, int], None],
        database_url: str = "sqlite:///fitness.db",
    ):
        self.on_bpm = on_bpm_update
        self.db = DatabaseManager(database_url=database_url)
        self.loop = asyncio.new_event_loop()
        self.queue: asyncio.Queue = asyncio.Queue()
        self._recording = False
        self._activity_id = None
        self._start_ns = None

    def start(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def start_recording(self):
        if not self._recording:
            self._activity_id = self.db.start_activity()
            self._recording = True
            self._start_ns = None

    def stop_recording(self):
        if self._recording:
            self.db.stop_activity(self._activity_id)
            self._recording = False

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._workflow())

    async def _workflow(self):
        dev = await scan_polar()
        if not dev:
            GLib.idle_add(self.on_bpm, 0.0, -1)
            return

        # start BLE stream
        ble_task = self.loop.create_task(
            connect_and_stream(dev, self.queue, lambda: None)
        )

        while True:
            tag, *data = await self.queue.get()
            if tag == "QUIT":
                break
            t_ns, (bpm, rr), energy = data
            if self._start_ns is None:
                self._start_ns = t_ns
            t_ns_zero = t_ns - self._start_ns
            t_sec = t_ns_zero / 1e9
            GLib.idle_add(self.on_bpm, t_sec, bpm)
            if self._recording:
                self.db.insert_heart_rate(self._activity_id, t_ns_zero, bpm, rr, energy)
        await ble_task

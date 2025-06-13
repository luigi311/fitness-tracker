import asyncio
import threading
from .ble import scan_polar, connect_and_stream
from .database import DatabaseManager
from typing import Callable
from gi.repository import GLib

class Recorder:
    """Orchestrates BLE streaming, DB writes, and UI callbacks."""
    def __init__(self, on_bpm_update: Callable[[float, int], None]):
        # on_bpm_update signature now: (seconds_since_start, bpm)
        self.on_bpm = on_bpm_update
        self.db = DatabaseManager()
        self.loop = asyncio.new_event_loop()
        self.queue: asyncio.Queue = asyncio.Queue()
        self._stop_event = asyncio.Event()
        self._recording = False
        self._activity_id: int | None = None
        self._start_ns: int | None = None

    def start(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def start_recording(self):
        if not self._recording:
            self._activity_id = self.db.start_activity()
            self._recording = True

    def stop_recording(self):
        if self._recording:
            self.db.stop_activity(self._activity_id)
            self.db.commit()
            self._activity_id = None
            self._recording = False

    def _on_disconnect(self):
        self._stop_event.set()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._workflow())

    async def _workflow(self):
        dev = await scan_polar()
        if not dev:
            # signal 'not found' with negative bpm
            GLib.idle_add(self.on_bpm, 0.0, -1)
            return

        ble_task = self.loop.create_task(
            connect_and_stream(dev, self.queue, self._on_disconnect)
        )

        while True:
            tag, *data = await self.queue.get()
            if tag == 'QUIT':
                break

            t_ns, (bpm, rr), energy = data
            # initialize start_ns
            if self._start_ns is None:
                self._start_ns = t_ns
            # compute seconds since start
            t_ns_zeroed = t_ns - self._start_ns
            if t_ns_zeroed < 0:
                # if we get a negative timestamp, skip this frame
                continue
            t_sec = t_ns_zeroed / 1e9

            # update UI with relative time
            GLib.idle_add(self.on_bpm, t_sec, bpm)

            # write to DB if recording (store raw t_ns)
            if self._recording:
                self.db.insert_heart_rate(
                    self._activity_id, t_ns_zeroed, bpm, rr, energy
                )
                if self.queue.qsize() % 20 == 0:
                    self.db.commit()

        await ble_task
        if self._recording:
            self.stop_recording()
        self.db.commit()
        self.db.close()
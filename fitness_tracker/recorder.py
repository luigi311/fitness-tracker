import asyncio
import threading
from .ble import scan_polar, connect_and_stream
from .database import DatabaseManager
from gi.repository import GLib

class Recorder:
    """Orchestrates BLE streaming, DB writes, and UI callbacks."""
    def __init__(self, on_bpm_update: callable):
        self.on_bpm = on_bpm_update
        self.db = DatabaseManager()
        self.loop = asyncio.new_event_loop()
        self.queue: asyncio.Queue = asyncio.Queue()
        self._stop_event = asyncio.Event()
        self._recording = False
        self._activity_id: int | None = None

    def start(self):
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()

    def start_recording(self):
        if not self._recording:
            self._activity_id = self.db.start_activity()
            self._recording = True

    def stop_recording(self):
        if self._recording:
            self.db.stop_activity(self._activity_id)
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
            GLib.idle_add(self.on_bpm, "Polar not found")
            return

        # kick off BLE stream
        ble_task = self.loop.create_task(
            connect_and_stream(dev, self.queue, self._on_disconnect)
        )

        # consume frames
        while True:
            tag, *data = await self.queue.get()
            if tag == 'QUIT':
                break

            # data = timestamp_ns, (bpm, rr), energy
            t_ns, (bpm, rr), energy = data
            GLib.idle_add(self.on_bpm, str(bpm))

            if self._recording:
                self.db.insert_heart_rate(
                    self._activity_id,
                    t_ns,
                    bpm,
                    rr,
                    energy,
                )
                # batch commit every 20
                if self.queue.qsize() % 20 == 0:
                    self.db.commit()

        # cleanup
        await ble_task
        if self._recording:
            self.stop_recording()
        self.db.commit()
        self.db.close()
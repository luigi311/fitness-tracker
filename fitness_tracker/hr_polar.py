import asyncio
from typing import AsyncGenerator, Callable

from backoff import expo, on_exception
from bleak import BleakClient, BleakError
from bleakheart import HeartRate

from fitness_tracker.hr_provider import HeartRateProvider


class PolarProvider(HeartRateProvider):
    name = "Polar"

    @staticmethod
    def matches(name: str) -> bool:
        return "polar" in name.lower()

    @staticmethod
    @on_exception(expo, BleakError, max_tries=3)
    async def _connect(device, disconnected_callback):
        """
        Try to connect to the device, retrying on BleakError up to 3 times.
        Attach the provided disconnected_callback to the client so we get
        notified immediately if the belt drops.
        """
        client = BleakClient(device, disconnected_callback=disconnected_callback)
        await client.connect()
        return client

    @staticmethod
    async def stream(
        device, on_disconnect: Callable[[str], None]
    ) -> AsyncGenerator[tuple[int, int, int, float], None]:
        """
        Async generator yielding (t_ms, bpm, rr, energy).
        - Retries connection on BleakError via backoff.
        - Calls on_disconnect(...) immediately on BLE disconnect.
        - Enqueues a ("QUIT",) so our loop unblocks and returns.
        """
        # 1) Prepare a single queue for both data and quit signals
        q: asyncio.Queue = asyncio.Queue()

        # 2) Define BLE disconnect handler
        def _bleak_disc(_client):
            # notify the UI
            on_disconnect("⚠️  Device disconnected")
            # unblock our read loop
            q.put_nowait(("QUIT",))

        # 3) Connect (with retries) and attach our disconnect callback
        try:
            client = await PolarProvider._connect(device, disconnected_callback=_bleak_disc)
        except BleakError as e:
            on_disconnect(f"❌  Failed to connect to {device.name}: {e}")
            return

        # 4) Start heart‐rate notifications into our queue
        hr = HeartRate(client, queue=q, instant_rate=True, unpack=True)
        await hr.start_notify()

        # 5) Consume queue and yield samples
        try:
            while True:
                event = await q.get()
                if event[0] == "QUIT":
                    break

                # unpack ("DATA", t_ns, (bpm, rr), energy)
                _, t_ns, (bpm, rr), energy = event
                t_ms = t_ns // 1_000_000
                yield t_ms, bpm, rr, energy

        finally:
            await client.disconnect()
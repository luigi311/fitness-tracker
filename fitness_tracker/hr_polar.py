from bleak import BleakScanner, BleakClient
from bleakheart import HeartRate
from fitness_tracker.hr_provider import HeartRateProvider
import asyncio


class PolarProvider(HeartRateProvider):
    name = "Polar"

    @staticmethod
    def matches(name: str) -> bool:
        return "polar" in name.lower()

    @staticmethod
    async def connect_and_stream(device, frame_queue, on_disconnect):
        """
        Connect to Polar, decode raw BLE into (t_ns, bpm, rr, energy),
        apply any Polar-specific scaling/unpacking, then emit:
           ("SAMPLE", t_ms, bpm, rr, energy)
        """
        def _on_disc(client):
            on_disconnect()

        # 1) Use a private queue so we can normalize each event
        internal_q: asyncio.Queue = asyncio.Queue()

        client = BleakClient(device, disconnected_callback=_on_disc)
        await client.connect()
        # tell HeartRate to push into our internal_q
        hr = HeartRate(client, queue=internal_q, instant_rate=True, unpack=True)
        await hr.start_notify()

        try:
            while True:
                event = await internal_q.get()

                # Assume event == ("QUIT",) on disconnect
                if event[0] == "QUIT":
                    # bubble up the quit
                    frame_queue.put_nowait(("QUIT",))
                    break

                # unpack the raw payload
                _, t_ns, (bpm, rr), energy = event

                # Convert to milliseconds
                t_ms = t_ns // 1_000_000

                # re-emit with a uniform tag
                frame_queue.put_nowait(("SAMPLE", t_ms, bpm, rr, energy))

        finally:
            await client.disconnect()
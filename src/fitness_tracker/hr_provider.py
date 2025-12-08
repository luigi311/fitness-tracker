import asyncio
from collections.abc import AsyncGenerator, Callable

from bleak import BleakClient, BleakError
from bleakheart import HeartRate

HEART_RATE_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"


# 16-bit SIG-assigned UUID for the standard Heart Rate Service
async def connect_and_stream(
    device,
    frame_queue: asyncio.Queue,
    on_disconnect: Callable[[str], None],
) -> AsyncGenerator[tuple[int, int, int, float]] | None:
    """
    Connect to a BLE heart-rate device and stream data tuples
    of (timestamp_ms, bpm, rr_interval, energy_kj).
    """

    # Handler for BLE disconnect events
    def _bleak_disconnect(client):
        on_disconnect("⚠️  Device disconnected")
        frame_queue.put_nowait(("QUIT",))

    # Establish BLE connection
    try:
        client = BleakClient(device, disconnected_callback=_bleak_disconnect)
        await client.connect()
    except BleakError as e:
        on_disconnect(f"❌  Failed to connect to {getattr(device, 'name', device)}: {e}")
        return

    # Subscribe to Heart Rate Service notifications
    hr = HeartRate(client, queue=frame_queue, instant_rate=True, unpack=True)
    await hr.start_notify()

    # Consume frame_queue and yield parsed measurements
    try:
        while True:
            event = await frame_queue.get()
            if event[0] == "QUIT":
                break

            # unpack ("DATA", t_ns, (bpm, rr), energy)
            _, t_ns, (bpm, rr), energy = event
            t_ms = t_ns // 1_000_000
            yield t_ms, bpm, rr, energy
    finally:
        await client.disconnect()

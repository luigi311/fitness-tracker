import asyncio
from bleak import BleakScanner, BleakClient
from bleakheart import HeartRate

UNPACK = True
INSTANT_RATE = True


async def scan_polar():
    """Return the first BLE device whose name contains 'polar'."""
    return await BleakScanner.find_device_by_filter(
        lambda d, adv: d.name and "polar" in d.name.lower()
    )


async def connect_and_stream(
    device, frame_queue: asyncio.Queue, on_disconnect: callable
):
    """
    Connect to device, push frames to queue, call on_disconnect on drop.
    """

    def _on_disc(client):
        on_disconnect()

    client = BleakClient(device, disconnected_callback=_on_disc)
    try:
        await client.connect()
        hr = HeartRate(
            client, queue=frame_queue, instant_rate=INSTANT_RATE, unpack=UNPACK
        )
        await hr.start_notify()

        while True:
            await asyncio.sleep(1)

    finally:
        await client.disconnect()
        frame_queue.put_nowait(("QUIT",))

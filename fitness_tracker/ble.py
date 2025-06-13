import asyncio
from bleak import BleakScanner, BleakClient
from bleakheart import HeartRate

UNPACK = True
INSTANT_RATE = True

async def scan_polar() -> BleakClient:
    """Find and return the first Polar BLE device."""
    return await BleakScanner.find_device_by_filter(
        lambda d, adv: d.name and 'polar' in d.name.lower()
    )

async def connect_and_stream(
    device,
    frame_queue: asyncio.Queue,
    on_disconnect: callable
):
    """
    Connect to the BLE device, push HR frames into frame_queue,
    call on_disconnect() if the sensor drops.
    """
    def _handle_disconnect(client):
        on_disconnect()

    client = BleakClient(device, disconnected_callback=_handle_disconnect)
    try:
        await client.connect()
        hr = HeartRate(
            client,
            queue=frame_queue,
            instant_rate=INSTANT_RATE,
            unpack=UNPACK,
        )
        await hr.start_notify()

        # stream until someone cancels the task
        while True:
            await asyncio.sleep(1)

    finally:
        try:
            await client.disconnect()
        except EOFError:
            pass
        # signal end
        frame_queue.put_nowait(('QUIT',))
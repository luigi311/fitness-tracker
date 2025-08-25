import threading
import time
from uuid import UUID

from libpebble2.communication import PebbleConnection
from libpebble2.communication.transports.qemu import QemuTransport
from libpebble2.communication.transports.serial import SerialTransport
from libpebble2.communication.transports.websocket import WebsocketTransport
from libpebble2.services.appmessage import AppMessageService, Uint8, Uint16, Uint32

KEY_HR = 1
KEY_PACE = 2
KEY_CADENCE = 3
KEY_DISTANCE = 4
KEY_STATUS = 5
KEY_UNITS = 6
KEY_POWER = 7


class PebbleBridge:
    """Bridge to a Pebble smartwatch (or emulator) via AppMessage."""

    def __init__(
        self,
        mac: str,
        app_uuid: str,
        send_hz: float = 1.0,
        *,
        use_emulator: bool = False,
    ) -> None:
        self.mac = mac
        self.app_uuid = app_uuid
        self.period = max(0.1, 1.0 / send_hz)
        self._lock = threading.Lock()
        self._state = {}  # latest metrics
        self._running = False
        self._t = None
        self._conn = None
        self._appmsg = None
        self.use_emulator = use_emulator

    def start(self) -> None:
        """Start the background thread to send updates."""
        self._running = True
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def stop(self) -> None:
        """Stop the background thread and disconnect."""
        self._running = False
        if self._t:
            self._t.join(timeout=1.0)
        try:
            if self._conn:
                self._conn.close()
        except Exception as e:
            print("PebbleBridge close error:", repr(e))

    def update(
        self,
        hr: int | None = None,
        pace_ms: float | None = None,
        cadence: int | None = None,
        dist_m: int | None = None,
        status: int | None = None,
        power_w: int | None = None,
        units: int | None = None,
    ) -> None:
        """Update the latest metrics (None = no change)."""
        with self._lock:
            if hr is not None:
                self._state[KEY_HR] = int(hr)
            if pace_ms is not None:
                self._state[KEY_PACE] = round(pace_ms * 100)
            if cadence is not None:
                self._state[KEY_CADENCE] = int(cadence)
            if dist_m is not None:
                self._state[KEY_DISTANCE] = int(dist_m)
            if status is not None:
                self._state[KEY_STATUS] = int(status)
            if units is not None:
                self._state[KEY_UNITS] = int(units)  # 0 metric, 1 imperial (optional)
            if power_w is not None:
                self._state[KEY_POWER] = int(power_w)

    # --- internal ---
    def _connect(self) -> None:
        if self._conn:
            return
        if self.use_emulator:
            # Try WS first (pypkjs), then fall back to QEMU
            try:
                self._conn = PebbleConnection(WebsocketTransport("ws://127.0.0.1:49053/"))
                self._conn.connect()
            except Exception as _:
                self._conn = PebbleConnection(QemuTransport("127.0.0.1", 47527))
                self._conn.connect()
        else:
            if not self.mac or self.mac == "00:00:00:00:00:00":
                msg = "Invalid MAC address for real Pebble"
                raise ValueError(msg)

            self._conn = PebbleConnection(SerialTransport("/dev/rfcomm0"))
        self._conn.connect()
        self._conn.run_async()
        self._appmsg = AppMessageService(self._conn)

    def _send_once(self, *, full: bool = False) -> None:
        with self._lock:
            if not self._state:
                return
            payload = dict(self._state) if full else self._state

        if not self._appmsg:
            return

        d = {}
        if KEY_HR in payload:
            d[KEY_HR] = Uint16(payload[KEY_HR])
        if KEY_PACE in payload:
            d[KEY_PACE] = Uint16(payload[KEY_PACE])
        if KEY_CADENCE in payload:
            d[KEY_CADENCE] = Uint16(payload[KEY_CADENCE])
        if KEY_DISTANCE in payload:
            d[KEY_DISTANCE] = Uint32(payload[KEY_DISTANCE])
        if KEY_STATUS in payload:
            d[KEY_STATUS] = Uint8(payload[KEY_STATUS])
        if KEY_UNITS in payload:
            d[KEY_UNITS] = Uint8(payload[KEY_UNITS])
        if KEY_POWER in payload:
            d[KEY_POWER] = Uint16(payload[KEY_POWER])

        self._appmsg.send_message(UUID(self.app_uuid), d)

    def _loop(self) -> None:
        backoff = 1.0
        full_after_reconnect = False
        while self._running:
            try:
                if not self._conn:
                    self._connect()
                    full_after_reconnect = True
                    backoff = 1.0
                self._send_once(full=full_after_reconnect)
                full_after_reconnect = False
                time.sleep(self.period)
            except Exception as e:
                print("PebbleBridge error:", repr(e))
                try:
                    if self._conn:
                        self._conn.close()
                except Exception as ee:
                    print("PebbleBridge close error:", repr(ee))
                self._conn = None
                self._appmsg = None
                time.sleep(backoff)
                backoff = min(10.0, backoff * 2)

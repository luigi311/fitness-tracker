from __future__ import annotations

import asyncio
import threading
import time
from uuid import UUID

from libpebble2.communication import PebbleConnection
from libpebble2.communication.transports.qemu import QemuTransport
from libpebble2.communication.transports.serial import SerialTransport
from libpebble2.communication.transports.websocket import WebsocketTransport
from libpebble2.services.appmessage import AppMessageService, Uint8, Uint16, Uint32
from loguru import logger

# cobble_client talks to the long-lived cobbled daemon over D-Bus. It has
# no BLE/BlueZ requirement itself (the daemon owns the radio), but it is still
# Linux/D-Bus oriented and may not be installed; degrade gracefully if so.
try:
    from cobble_client import CobbleClient as _CobbleClient
    from cobble_client import DaemonNotRunningError
    from cobble_client import u8 as _cobble_u8
    from cobble_client import u16 as _cobble_u16
    from cobble_client import u32 as _cobble_u32

    HAVE_COBBLE = True
except (ImportError, RuntimeError) as _e:  # pragma: no cover - platform dependent
    HAVE_COBBLE = False
    _COBBLE_UNAVAILABLE_REASON = repr(_e)

KEY_HR = 1
KEY_SPEED = 2
KEY_CADENCE = 3
KEY_DISTANCE = 4
KEY_STATUS = 5
KEY_UNITS = 6
KEY_POWER = 7
KEY_TGT_KIND = 8
KEY_TGT_LO = 9
KEY_TGT_HI = 10
KEY_WORKOUT_OUTDOOR = 11
KEY_WORKOUT_STEP = 12

# The EXACT widths the watchapp's C handler reads each key as
# (t->value->uint8/uint16/uint32). Every backend pins to these so the watch
# decodes correctly regardless of transport.
_KEY_WIDTH = {
    KEY_HR: 16,
    KEY_SPEED: 16,
    KEY_CADENCE: 16,
    KEY_DISTANCE: 32,
    KEY_STATUS: 8,
    KEY_UNITS: 8,
    KEY_POWER: 16,
    KEY_TGT_KIND: 8,
    KEY_TGT_LO: 16,
    KEY_TGT_HI: 16,
    KEY_WORKOUT_OUTDOOR: 8,
    KEY_WORKOUT_STEP: 16,
}


class _CobbleBackend:
    """cobble_client transport: send via the shared cobbled daemon.

    Preferred path. The daemon already owns one BLE link to the watch, so this
    backend never touches the radio — it just proxies AppMessages over D-Bus.
    That also means multiple processes can drive the watch at once (the whole
    reason the daemon exists).

    This is a blocking facade over a dedicated asyncio loop thread because
    CobbleClient is fully async and the bridge's sender thread
    calls connect()/send()/close() synchronously.
    """

    name = "cobble"

    def __init__(
        self,
        app_uuid: str,
        connect_timeout: float = 10.0,
        send_timeout: float = 10.0,
    ) -> None:
        self._app_uuid = app_uuid
        self._connect_timeout = connect_timeout
        self._send_timeout = send_timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client = None
        # Width -> wrapper. These are the client's wrappers (cobble_client
        # re-exports them); the client encodes them through its D-Bus codec so
        # the width pin survives the D-Bus hop to the daemon.
        self.int_types = {8: _cobble_u8, 16: _cobble_u16, 32: _cobble_u32}

    def connect(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="cobble-client-loop",
            daemon=True,
        )
        self._thread.start()
        try:
            self._call(self._async_connect(), timeout=self._connect_timeout + 10.0)
        except BaseException:
            self.close()
            raise

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _call(self, coro, timeout: float):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    async def _async_connect(self) -> None:
        client = _CobbleClient()
        # require_daemon=True makes connect() raise DaemonNotRunningError
        # immediately if the daemon's bus name has no owner, instead of
        # blocking — that exception is our signal to fall through to the next
        # backend.
        await client.connect(require_daemon=True)
        self._client = client
        # Bring the watchapp to the foreground; an app that isn't running just
        # NACKs every AppMessage. Best-effort: it may already be open.
        try:
            await self._client.launch_app(self._app_uuid)
            await asyncio.sleep(1.0)
        except Exception as e:
            logger.debug(f"daemon launch_app failed (app may already be open): {e!r}")

    def send(self, data: dict) -> None:
        if self._client is None or self._loop is None:
            msg = "daemon backend not connected"
            raise RuntimeError(msg)
        # wait_ack=True blocks until the watch ACKs THIS message. Now that the
        # 0xff/0x7f ACK encoding is recognized, this actually resolves, so the
        # bridge self-throttles to the watch's real drain rate: at most one
        # message in flight, so the multi-second backlog can't build and the
        # watch shows fresh data (one round-trip of latency) instead of lagging
        # several seconds behind. Latest-wins is preserved because _send_once
        # always sends the current state snapshot.
        self._call(
            self._client.send_app_message(self._app_uuid, data, wait_ack=True),
            timeout=self._send_timeout,
        )

    def close(self) -> None:
        loop, self._loop = self._loop, None
        if loop is None:
            return
        if self._client is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._client.close(), loop).result(10.0)
            except Exception as e:
                logger.debug(f"daemon close error: {e!r}")
            self._client = None
        loop.call_soon_threadsafe(loop.stop)
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        if not loop.is_running():
            loop.close()


class _Libpebble2Backend:
    """libpebble2 transport: Bluetooth serial (real watch) or WS/QEMU (emulator)."""

    def __init__(
        self,
        app_uuid: str,
        mac: str | None = None,
        *,
        use_emulator: bool = False,
        port: int = 47527,
    ) -> None:
        self._app_uuid = app_uuid
        self._mac = mac
        self._use_emulator = use_emulator
        self._port = port
        self._conn: PebbleConnection | None = None
        self._appmsg: AppMessageService | None = None
        self.name = "emulator" if use_emulator else "serial"
        self.int_types = {8: Uint8, 16: Uint16, 32: Uint32}

    def connect(self) -> None:
        if self._use_emulator:
            logger.debug("Connecting via emulator")
            # Try WS first (pypkjs), then fall back to QEMU
            try:
                self._conn = PebbleConnection(WebsocketTransport(f"ws://127.0.0.1:{self._port}/"))
            except Exception:
                self._conn = PebbleConnection(QemuTransport("127.0.0.1", self._port))
        else:
            if not self._mac:
                msg = "Invalid MAC address for real Pebble"
                raise ValueError(msg)
            logger.debug(f"Connecting via Bluetooth serial: {self._mac}")
            self._conn = PebbleConnection(SerialTransport(self._mac))

        self._conn.connect()
        self._conn.run_async()

        if not self._conn.connected:
            # Raise instead of limping on so the bridge loop's backoff/retry
            # (and a future BLE re-attempt) actually kicks in.
            msg = f"libpebble2 ({self.name}) failed to connect"
            raise RuntimeError(msg)

        self._appmsg = AppMessageService(self._conn)

    def send(self, data: dict) -> None:
        if self._appmsg is None:
            msg = f"{self.name} backend not connected"
            raise RuntimeError(msg)
        self._appmsg.send_message(UUID(self._app_uuid), data)

    def close(self) -> None:
        conn, self._conn = self._conn, None
        self._appmsg = None
        if conn:
            conn.close()


class PebbleBridge:
    """Bridge to a Pebble smartwatch (or emulator) via AppMessage.

    Real watch, in fallback order:
      1. cobbled daemon (cobble_client over D-Bus) — preferred, shares
         the daemon's single BLE link so multiple processes can coexist.
      2. libpebble2 Bluetooth serial — fallback when cobbled is unavailable.
    Emulator: libpebble2 WS/QEMU as before.
    """

    def __init__(
        self,
        app_uuid: str,
        mac: str | None = None,
        *,
        use_emulator: bool = False,
        port: int = 47527,
    ) -> None:
        self.mac = mac
        self.app_uuid = app_uuid
        self.use_emulator = use_emulator
        self.port = port
        self._lock = threading.Lock()
        self._state = {}  # latest metrics, key -> plain int
        self._running = False
        self._t: threading.Thread | None = None
        self._backend: _CobbleBackend | _Libpebble2Backend | None = None

    def start(self) -> None:
        """Start the background thread to send updates."""
        mode = "Emulator" if self.use_emulator else "Watch"
        logger.debug(f"Starting pebble bridge ({mode})")
        self._running = True
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def stop(self) -> None:
        """Stop the background thread and disconnect."""
        self._running = False
        if self._t:
            self._t.join(timeout=1.0)
            self._t = None
        self._close_backend()

    def update(
        self,
        hr: int | None = None,
        speed_mps: float | None = None,
        cadence: int | None = None,
        dist_m: int | None = None,
        status: int | None = None,
        power_w: int | None = None,
        units: int | None = None,
        tgt_kind: int | None = None,
        tgt_lo: float | None = None,
        tgt_hi: float | None = None,
        workout_outdoor: bool | None = None,
        workout_step: int | None = None,
    ) -> None:
        """Update the latest metrics (None = no change)."""
        with self._lock:
            if hr is not None:
                self._state[KEY_HR] = int(hr)
            if speed_mps is not None:
                self._state[KEY_SPEED] = round(speed_mps * 100)
            if cadence is not None:
                self._state[KEY_CADENCE] = int(cadence)
            if dist_m is not None:
                self._state[KEY_DISTANCE] = int(dist_m)
            if status is not None:
                self._state[KEY_STATUS] = int(status)
            if units is not None:
                # 0 metric, 1 imperial (optional)
                self._state[KEY_UNITS] = int(units)
            if power_w is not None:
                self._state[KEY_POWER] = int(power_w)
            if tgt_kind is not None:
                # 0 none, 1 power, 2 pace
                self._state[KEY_TGT_KIND] = int(tgt_kind)
            if tgt_lo is not None:
                # Power/HR use whole units; pace uses m/s * 100.
                val = round(tgt_lo if tgt_kind in (1, 3) else (tgt_lo * 100.0))
                self._state[KEY_TGT_LO] = val
            if tgt_hi is not None:
                val = round(tgt_hi if tgt_kind in (1, 3) else (tgt_hi * 100.0))
                self._state[KEY_TGT_HI] = val
            if workout_outdoor is not None:
                self._state[KEY_WORKOUT_OUTDOOR] = int(workout_outdoor)
            if workout_step is not None:
                self._state[KEY_WORKOUT_STEP] = int(workout_step)

    # --- internal ---
    def _connect(self) -> None:
        if self._backend:
            logger.debug("Already connected")
            return

        # Emulator: libpebble2 WS/QEMU, exactly as before.
        if self.use_emulator:
            backend = _Libpebble2Backend(self.app_uuid, use_emulator=True, port=self.port)
            backend.connect()
            self._backend = backend
            logger.success("Connected to Pebble (emulator)")
            return

        if not self.mac:
            msg = "Invalid MAC address for real Pebble"
            raise ValueError(msg)

        # Real watch, fallback order: cobbled daemon -> serial.

        # 1. Shared daemon (cobble_client). Preferred when one is running.
        if HAVE_COBBLE:
            backend = _CobbleBackend(self.app_uuid)
            try:
                backend.connect()
            except DaemonNotRunningError as e:
                logger.debug(f"no cobbled daemon running ({e!r}); using Bluetooth serial")
                backend.close()

            except Exception as e:
                logger.warning(f"cobbled connect failed ({e!r}); using Bluetooth serial")
                backend.close()

            else:
                self._backend = backend
                logger.success("Connected to Pebble via cobbled daemon")
                return
        else:
            logger.debug(
                f"cobble_client unavailable ({_COBBLE_UNAVAILABLE_REASON}); using Bluetooth serial",
            )

        # 2. Serial fallback (libpebble2).
        backend = _Libpebble2Backend(self.app_uuid, mac=self.mac)
        backend.connect()
        self._backend = backend
        logger.success("Connected to Pebble over Bluetooth serial")

    def _close_backend(self) -> None:
        backend, self._backend = self._backend, None
        if backend:
            try:
                backend.close()
            except Exception as e:
                logger.error(f"PebbleBridge close error: {e!r}")

    def _send_once(self, *, full: bool = False) -> None:
        with self._lock:
            if not self._state:
                return
            payload = dict(self._state)  # snapshot; never iterate live dict unlocked

        backend = self._backend
        if not backend:
            return

        # Pin every value to the exact width the watchapp reads, using
        # whichever wrapper types the active backend speaks.
        wrap = backend.int_types
        d = {key: wrap[_KEY_WIDTH.get(key, 16)](value) for key, value in payload.items()}
        backend.send(d)

    def _loop(self) -> None:
        backoff = 1.0
        full_after_reconnect = False
        while self._running:
            try:
                if not self._backend:
                    self._connect()
                    full_after_reconnect = True
                    backoff = 1.0
                # Re-check after a (possibly slow) connect: if stop() fired
                # while we were connecting, don't push a final stale frame.
                if not self._running:
                    break
                self._send_once(full=full_after_reconnect)
                full_after_reconnect = False
                time.sleep(0.5)
            except Exception as e:
                logger.error(f"PebbleBridge error: {e!r}")
                self._close_backend()
                time.sleep(backoff)
                backoff = min(10.0, backoff * 2)

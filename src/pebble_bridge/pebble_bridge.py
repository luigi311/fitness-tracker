from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from typing import TYPE_CHECKING
from uuid import UUID

from libpebble2.communication import PebbleConnection
from libpebble2.communication.transports.qemu import QemuTransport
from libpebble2.communication.transports.serial import SerialTransport
from libpebble2.communication.transports.websocket import WebsocketTransport
from libpebble2.services.appmessage import AppMessageService, Uint8, Uint16, Uint32
from loguru import logger

from pebble_bridge.protocol import (
    KEY_CADENCE,
    KEY_DISTANCE,
    KEY_HR,
    KEY_PACE,
    KEY_PACE_SCALE,
    KEY_POWER,
    KEY_SYNC_REQUEST,
    KEY_TGT_HI,
    KEY_TGT_KIND,
    KEY_TGT_LO,
    KEY_UNITS,
    KEY_WIDTHS,
    KEY_WORKOUT_OUTDOOR,
    KEY_WORKOUT_STEP,
    TARGET_KIND_SCALE,
    TGT_NONE,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

# cobble_client talks to the long-lived cobbled daemon over D-Bus. It has
# no BLE/BlueZ requirement itself (the daemon owns the radio), but it is still
# Linux/D-Bus oriented and may not be installed; degrade gracefully if so.
try:
    from cobble_client import CobbleClient as _CobbleClient
    from cobble_client import DaemonNotRunningError
    from cobble_client import Int as _CobbleInt
    from cobble_client import u8 as _cobble_u8
    from cobble_client import u16 as _cobble_u16
    from cobble_client import u32 as _cobble_u32

    HAVE_COBBLE = True
except (ImportError, RuntimeError) as _e:  # pragma: no cover - platform dependent
    HAVE_COBBLE = False
    _COBBLE_UNAVAILABLE_REASON = repr(_e)


def _clamp_wire_value(value: int, width: int) -> int:
    """Clamp an integer to the unsigned range of a Pebble AppMessage field."""
    return max(0, min(value, (1 << width) - 1))


def _target_wire_value(value: float, kind: int | None) -> int:
    """Encode a target using the canonical whole-unit or pace scaling rule."""
    target_kind = TGT_NONE if kind is None else kind
    scale = TARGET_KIND_SCALE.get(target_kind, TARGET_KIND_SCALE[TGT_NONE])
    return round(value * scale)


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
        on_app_message: Callable[[str, dict[int, object]], None],
        connect_timeout: float = 10.0,
        send_timeout: float = 10.0,
    ) -> None:
        self._app_uuid = app_uuid
        self._message_callback = on_app_message
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
        loop = self._loop
        if loop is None:
            message = "Pebble bridge event loop was not initialized"
            raise RuntimeError(message)
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def _call[T](
        self,
        coro: Coroutine[object, object, T],
        timeout: float,
    ) -> T:
        loop = self._loop
        if loop is None:
            message = "Pebble bridge is not connected"
            raise RuntimeError(message)
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

    async def _async_connect(self) -> None:
        client = _CobbleClient()
        # require_daemon=True makes connect() raise DaemonNotRunningError
        # immediately if the daemon's bus name has no owner, instead of
        # blocking — that exception is our signal to fall through to the next
        # backend.
        await client.connect(require_daemon=True)
        self._client = client
        client.on_app_message(self._handle_app_message)
        # Bring the watchapp to the foreground; an app that isn't running just
        # NACKs every AppMessage. Best-effort: it may already be open.
        try:
            await self._client.launch_app(self._app_uuid)
            await asyncio.sleep(1.0)
        except Exception as e:
            logger.debug(f"daemon launch_app failed (app may already be open): {e!r}")

    def _handle_app_message(self, app_uuid: str, data: dict[int, object]) -> None:
        value = data.get(KEY_SYNC_REQUEST)
        if isinstance(value, _CobbleInt):
            data = dict(data)
            data[KEY_SYNC_REQUEST] = value.value
        self._message_callback(app_uuid, data)

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
        on_app_message: Callable[[str, dict[int, object]], None],
        mac: str | None = None,
        *,
        use_emulator: bool = False,
        port: int = 47527,
    ) -> None:
        self._app_uuid = app_uuid
        self._message_callback = on_app_message
        self._mac = mac
        self._use_emulator = use_emulator
        self._port = port
        self._conn: PebbleConnection | None = None
        self._appmsg: AppMessageService | None = None
        self._send_timeout = 10.0
        self._delivery_condition = threading.Condition()
        self._delivery_results: dict[int, bool] = {}
        self._pending_transaction: int | None = None
        self._closed = False
        self.name = "emulator" if use_emulator else "serial"
        self.int_types = {8: Uint8, 16: Uint16, 32: Uint32}

    def connect(self) -> None:
        try:
            self._create_connection()
        except BaseException:
            with contextlib.suppress(Exception):
                self.close()
            raise

    def _create_connection(self) -> None:
        if self._use_emulator:
            logger.debug("Connecting via emulator")
            # Try WS first (pypkjs), then fall back to QEMU
            try:
                self._conn = PebbleConnection(
                    WebsocketTransport(f"ws://127.0.0.1:{self._port}/"),
                )
                self._initialize_connection()
            except Exception:
                connection, self._conn = self._conn, None
                if connection is not None:
                    with contextlib.suppress(Exception):
                        connection.close()
                self._appmsg = None
                self._conn = PebbleConnection(QemuTransport("127.0.0.1", self._port))
                self._initialize_connection()
            return

        if not self._mac:
            msg = "Invalid MAC address for real Pebble"
            raise ValueError(msg)
        logger.debug(f"Connecting via Bluetooth serial: {self._mac}")
        self._conn = PebbleConnection(SerialTransport(self._mac))
        self._initialize_connection()

    def _initialize_connection(self) -> None:
        conn = self._conn
        if conn is None:
            msg = f"libpebble2 ({self.name}) connection was not created"
            raise RuntimeError(msg)
        conn.connect()
        conn.run_async()

        if not conn.connected:
            # Raise instead of limping on so the bridge loop's backoff/retry
            # (and a future BLE re-attempt) actually kicks in.
            msg = f"libpebble2 ({self.name}) failed to connect"
            raise RuntimeError(msg)

        self._appmsg = AppMessageService(conn)
        self._appmsg.register_handler("appmessage", self._handle_app_message)
        self._appmsg.register_handler("ack", self._handle_ack)
        self._appmsg.register_handler("nack", self._handle_nack)
        with self._delivery_condition:
            self._closed = False
            self._delivery_results.clear()
            self._pending_transaction = None

    def _handle_app_message(
        self,
        _transaction_id: int,
        app_uuid: UUID,
        data: dict[int, object],
    ) -> None:
        self._message_callback(str(app_uuid), data)

    def _record_delivery(self, transaction_id: int, *, acknowledged: bool) -> None:
        with self._delivery_condition:
            if not self._closed and transaction_id == self._pending_transaction:
                self._delivery_results[transaction_id] = acknowledged
                self._delivery_condition.notify_all()

    def _handle_ack(self, transaction_id: int, _app_uuid: UUID | None) -> None:
        self._record_delivery(transaction_id, acknowledged=True)

    def _handle_nack(self, transaction_id: int, _app_uuid: UUID | None) -> None:
        self._record_delivery(transaction_id, acknowledged=False)

    def send(self, data: dict) -> None:
        appmsg = self._appmsg
        if appmsg is None:
            msg = f"{self.name} backend not connected"
            raise RuntimeError(msg)
        with self._delivery_condition:
            transaction_id = appmsg.send_message(UUID(self._app_uuid), data)
            deadline = time.monotonic() + self._send_timeout
            self._delivery_results.pop(transaction_id, None)
            self._pending_transaction = transaction_id
            try:
                while transaction_id not in self._delivery_results and not self._closed:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        msg = f"{self.name} AppMessage transaction {transaction_id} timed out"
                        raise TimeoutError(msg)
                    self._delivery_condition.wait(timeout=remaining)
                if self._closed:
                    msg = f"{self.name} backend closed before AppMessage acknowledgement"
                    raise RuntimeError(msg)
                acknowledged = self._delivery_results.pop(transaction_id)
            finally:
                if self._pending_transaction == transaction_id:
                    self._pending_transaction = None
                self._delivery_results.pop(transaction_id, None)
        if not acknowledged:
            msg = f"{self.name} AppMessage transaction {transaction_id} was rejected"
            raise RuntimeError(msg)

    def close(self) -> None:
        conn, self._conn = self._conn, None
        with self._delivery_condition:
            self._closed = True
            self._pending_transaction = None
            self._delivery_results.clear()
            self._delivery_condition.notify_all()
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
        self._wake = threading.Event()
        self._state: dict[int, int] = {}  # latest metrics, key -> plain int
        self._dirty_keys: set[int] = set()
        self._full_request_id = 0
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
        self._wake.set()
        self._close_backend()
        thread = self._t
        if thread:
            thread.join(timeout=1.0)
            if not thread.is_alive():
                self._t = None

    def _update_metrics(
        self,
        *,
        hr: int | None,
        speed_mps: float | None,
        cadence: int | None,
        dist_m: int | None,
        power_w: int | None,
        units: int | None,
    ) -> None:
        """Apply metric and display-unit changes; caller must hold ``self._lock``."""
        if hr is not None:
            self._set_state(KEY_HR, int(hr))
        if speed_mps is not None:
            self._set_state(KEY_PACE, round(speed_mps * KEY_PACE_SCALE))
        if cadence is not None:
            self._set_state(KEY_CADENCE, int(cadence))
        if dist_m is not None:
            self._set_state(KEY_DISTANCE, int(dist_m))
        if units is not None:
            # 0 metric, 1 imperial (optional)
            self._set_state(KEY_UNITS, int(units))
        if power_w is not None:
            self._set_state(KEY_POWER, int(power_w))

    def _update_target_state(
        self,
        *,
        tgt_kind: int | None,
        tgt_lo: float | None,
        tgt_hi: float | None,
    ) -> None:
        """Apply target-domain and target-band changes; caller must hold ``self._lock``."""
        if tgt_kind is not None:
            self._set_state(KEY_TGT_KIND, int(tgt_kind))
        target_kind = tgt_kind if tgt_kind is not None else self._state.get(KEY_TGT_KIND)
        if tgt_lo is not None:
            self._set_state(KEY_TGT_LO, _target_wire_value(tgt_lo, target_kind))
        if tgt_hi is not None:
            self._set_state(KEY_TGT_HI, _target_wire_value(tgt_hi, target_kind))

    def _update_workout_state(
        self,
        *,
        workout_outdoor: bool | None,
        workout_step: int | None,
    ) -> None:
        """Apply workout-state changes; caller must hold ``self._lock``."""
        if workout_outdoor is not None:
            self._set_state(KEY_WORKOUT_OUTDOOR, int(workout_outdoor))
        if workout_step is not None:
            self._set_state(KEY_WORKOUT_STEP, int(workout_step))

    def update(
        self,
        *,
        hr: int | None = None,
        speed_mps: float | None = None,
        cadence: int | None = None,
        dist_m: int | None = None,
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
            self._update_metrics(
                hr=hr,
                speed_mps=speed_mps,
                cadence=cadence,
                dist_m=dist_m,
                power_w=power_w,
                units=units,
            )
            self._update_target_state(tgt_kind=tgt_kind, tgt_lo=tgt_lo, tgt_hi=tgt_hi)
            self._update_workout_state(
                workout_outdoor=workout_outdoor,
                workout_step=workout_step,
            )

    # --- internal ---
    def _on_app_message(self, app_uuid: str, data: dict[int, object]) -> None:
        if app_uuid.casefold() != self.app_uuid.casefold():
            return
        if data.get(KEY_SYNC_REQUEST) != 1:
            return
        with self._lock:
            self._full_request_id += 1
        self._wake.set()

    def _set_state(self, key: int, value: int) -> None:
        if self._state.get(key) != value:
            self._dirty_keys.add(key)
        self._state[key] = value

    def _connect(self) -> None:
        if self._backend:
            logger.debug("Already connected")
            return

        # Emulator: libpebble2 WS/QEMU, exactly as before.
        if self.use_emulator:
            backend = _Libpebble2Backend(
                self.app_uuid,
                self._on_app_message,
                use_emulator=True,
                port=self.port,
            )
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
            backend = _CobbleBackend(self.app_uuid, self._on_app_message)
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
        backend = _Libpebble2Backend(self.app_uuid, self._on_app_message, mac=self.mac)
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
            payload = {
                key: value for key, value in self._state.items() if full or key in self._dirty_keys
            }
            if not payload:
                return

        backend = self._backend
        if not backend:
            return

        # Pin every value to the exact width the watchapp reads, using
        # whichever wrapper types the active backend speaks.
        wrap = backend.int_types
        d = {
            key: wrap[KEY_WIDTHS[key]](_clamp_wire_value(value, KEY_WIDTHS[key]))
            for key, value in payload.items()
        }
        backend.send(d)

        with self._lock:
            for key, value in payload.items():
                if self._state.get(key) == value:
                    self._dirty_keys.discard(key)

    def _loop(self) -> None:
        backoff = 1.0
        full_after_reconnect = False
        try:
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
                    with self._lock:
                        request_id = self._full_request_id
                    self._send_once(full=full_after_reconnect or request_id > 0)
                    if request_id:
                        with self._lock:
                            if self._full_request_id == request_id:
                                self._full_request_id = 0
                    full_after_reconnect = False
                    if self._running:
                        self._wake.wait(0.5)
                        self._wake.clear()
                except Exception as e:
                    if not self._running:
                        break
                    logger.error(f"PebbleBridge error: {e!r}")
                    self._close_backend()
                    if self._wake.wait(backoff):
                        self._wake.clear()
                    backoff = min(10.0, backoff * 2)
        finally:
            self._close_backend()

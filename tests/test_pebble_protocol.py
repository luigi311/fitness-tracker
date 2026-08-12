# ruff: noqa: PLR2004, SLF001

import json
import threading
from pathlib import Path

import pebble_bridge.pebble_bridge as pebble_module
import pebble_bridge.protocol as protocol_module
import pytest
from pebble_bridge import PebbleBridge

ROOT = Path(__file__).parents[1]
EXPECTED_MESSAGE_KEY_IDS = {
    "KEY_HR": 1,
    "KEY_PACE": 2,
    "KEY_CADENCE": 3,
    "KEY_DISTANCE": 4,
    "KEY_UNITS": 6,
    "KEY_POWER": 7,
    "KEY_TGT_KIND": 8,
    "KEY_TGT_LO": 9,
    "KEY_TGT_HI": 10,
    "KEY_WORKOUT_OUTDOOR": 11,
    "KEY_WORKOUT_STEP": 12,
    "KEY_SYNC_REQUEST": 13,
}


class _Backend:
    def __init__(self) -> None:
        self.messages: list[dict[int, tuple[int, int]]] = []
        self.fail_next = False
        self.closed = threading.Event()
        self.int_types = {
            width: (lambda value, width=width: (width, value)) for width in (8, 16, 32)
        }

    def send(self, message: dict[int, tuple[int, int]]) -> None:
        if self.fail_next:
            self.fail_next = False
            error_message = "send failed"
            raise RuntimeError(error_message)
        self.messages.append(message)

    def close(self) -> None:
        self.closed.set()


class _LibpebbleAppMessage:
    def __init__(self) -> None:
        self.sent = threading.Event()

    def send_message(self, *_args: object, **_kwargs: object) -> int:
        self.sent.set()
        return 7


def _sent_values(backend: _Backend) -> dict[int, int]:
    return {key: value[1] for key, value in backend.messages[-1].items()}


def _bridge_with_backend() -> tuple[PebbleBridge, _Backend]:
    bridge = PebbleBridge("00000000-0000-0000-0000-000000000000")
    backend = _Backend()
    bridge._backend = backend
    return bridge, backend


def test_python_c_and_manifest_use_the_same_protocol_keys() -> None:
    manifest = json.loads((ROOT / "pebble/package.json").read_text(encoding="utf-8"))
    protocol_header = (ROOT / "pebble/src/c/protocol.h").read_text(encoding="utf-8")
    generated_header = (ROOT / "pebble/src/c/generated_protocol.h").read_text(encoding="utf-8")

    assert pebble_module.KEY_PACE == 2
    assert not hasattr(pebble_module, "KEY_STATUS")
    assert manifest["pebble"]["messageKeys"] == EXPECTED_MESSAGE_KEY_IDS
    assert {
        name: getattr(pebble_module, name) for name in EXPECTED_MESSAGE_KEY_IDS
    } == EXPECTED_MESSAGE_KEY_IDS
    assert '#include "generated_protocol.h"' in protocol_header
    assert "KEY_STATUS" not in generated_header
    assert "#define KEY_HR_WIDTH 16" in generated_header
    assert "#define KEY_HR_C_TYPE uint16_t" in generated_header
    assert "#define KEY_PACE_SCALE 100" in generated_header
    assert "#define KEY_PACE_TUPLE_VALUE(tuple)" in generated_header


@pytest.mark.parametrize(
    ("updates", "key", "expected"),
    [
        ({"units": -1}, pebble_module.KEY_UNITS, 0),
        ({"power_w": 70_000}, pebble_module.KEY_POWER, 65_535),
        ({"dist_m": 2**40}, pebble_module.KEY_DISTANCE, 2**32 - 1),
        ({"tgt_kind": 1, "tgt_lo": -10.0}, pebble_module.KEY_TGT_LO, 0),
    ],
)
def test_payload_values_are_clamped_to_watch_wire_widths(
    updates: dict[str, int | float],
    key: int,
    expected: int,
) -> None:
    bridge, backend = _bridge_with_backend()
    bridge.update(**updates)

    bridge._send_once()

    assert _sent_values(backend)[key] == expected


@pytest.mark.parametrize(
    ("kind", "lower", "upper"),
    [
        (protocol_module.TGT_NONE, 2.5, 3.0),
        (protocol_module.TGT_POWER, 150.0, 200.0),
        (protocol_module.TGT_PACE, 2.5, 3.0),
        (protocol_module.TGT_HEART_RATE, 140.0, 160.0),
    ],
)
def test_target_values_use_kind_specific_wire_scaling(
    kind: int,
    lower: float,
    upper: float,
) -> None:
    bridge, backend = _bridge_with_backend()
    bridge.update(tgt_kind=kind, tgt_lo=lower, tgt_hi=upper)

    bridge._send_once()

    values = _sent_values(backend)
    assert values[pebble_module.KEY_TGT_KIND] == kind
    expected_scale = protocol_module.TARGET_KIND_SCALE[kind]
    assert values[pebble_module.KEY_TGT_LO] == round(lower * expected_scale)
    assert values[pebble_module.KEY_TGT_HI] == round(upper * expected_scale)


def test_steady_state_sends_only_changed_keys() -> None:
    bridge, backend = _bridge_with_backend()
    bridge.update(hr=140, speed_mps=2.5)

    bridge._send_once()
    assert set(_sent_values(backend)) == {
        pebble_module.KEY_HR,
        pebble_module.KEY_PACE,
    }

    message_count = len(backend.messages)
    bridge._send_once()
    assert len(backend.messages) == message_count

    bridge.update(hr=141)
    bridge._send_once()
    assert set(_sent_values(backend)) == {pebble_module.KEY_HR}


def test_failed_send_keeps_delta_dirty_for_retry() -> None:
    bridge, backend = _bridge_with_backend()
    bridge.update(hr=140)
    backend.fail_next = True

    with pytest.raises(RuntimeError, match="send failed"):
        bridge._send_once()

    bridge._send_once()
    assert set(_sent_values(backend)) == {pebble_module.KEY_HR}


def _libpebble_backend() -> tuple[pebble_module._Libpebble2Backend, _LibpebbleAppMessage]:
    backend = pebble_module._Libpebble2Backend(
        "00000000-0000-0000-0000-000000000000",
        lambda _app_uuid, _data: None,
    )
    appmessage = _LibpebbleAppMessage()
    backend._appmsg = appmessage
    backend._send_timeout = 0.1
    return backend, appmessage


def test_libpebble2_send_waits_for_ack() -> None:
    backend, appmessage = _libpebble_backend()
    errors: list[BaseException] = []

    def send() -> None:
        try:
            backend.send({})
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=send)
    thread.start()
    assert appmessage.sent.wait(timeout=1)
    assert thread.is_alive()

    backend._handle_ack(7, None)
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert errors == []


@pytest.mark.parametrize(
    ("event", "message"),
    [
        ("nack", "was rejected"),
        (None, "timed out"),
    ],
)
def test_libpebble2_send_retains_failure_until_delivery(
    event: str | None,
    message: str,
) -> None:
    backend, appmessage = _libpebble_backend()
    if event is None:
        backend._send_timeout = 0.001

    errors: list[BaseException] = []

    def send() -> None:
        try:
            backend.send({})
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=send)
    thread.start()
    assert appmessage.sent.wait(timeout=1)
    if event == "nack":
        backend._handle_nack(7, None)
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert message in str(errors[0])


@pytest.mark.parametrize("use_emulator", [False, True])
def test_libpebble2_connect_closes_connection_after_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
    use_emulator: bool,  # noqa: FBT001
) -> None:
    closed = threading.Event()

    class _FailingConnection:
        connected = False

        def connect(self) -> None:
            message = "transport connect failed"
            raise RuntimeError(message)

        def run_async(self) -> None:
            return None

        def close(self) -> None:
            closed.set()

    connection = _FailingConnection()
    monkeypatch.setattr(pebble_module, "PebbleConnection", lambda _transport: connection)
    if use_emulator:
        monkeypatch.setattr(pebble_module, "WebsocketTransport", lambda _url: object())
    else:
        monkeypatch.setattr(pebble_module, "SerialTransport", lambda _mac: object())

    backend = pebble_module._Libpebble2Backend(
        "00000000-0000-0000-0000-000000000000",
        lambda _app_uuid, _data: None,
        mac="AA:BB:CC:DD:EE:FF",
        use_emulator=use_emulator,
    )

    with pytest.raises(RuntimeError, match="transport connect failed"):
        backend.connect()

    assert closed.is_set()
    assert backend._conn is None


def test_stop_closes_libpebble2_ack_wait_before_joining() -> None:
    bridge = PebbleBridge("00000000-0000-0000-0000-000000000000")
    backend, appmessage = _libpebble_backend()
    backend._send_timeout = 10.0
    bridge._backend = backend
    bridge.update(hr=140)
    bridge._running = True
    worker = threading.Thread(target=bridge._loop)
    bridge._t = worker
    worker.start()

    assert appmessage.sent.wait(timeout=1)
    bridge.stop()

    assert not worker.is_alive()
    assert bridge._t is None


def test_stop_during_connect_closes_late_backend() -> None:
    bridge = PebbleBridge(
        "00000000-0000-0000-0000-000000000000",
        mac="AA:BB:CC:DD:EE:FF",
    )
    backend = _Backend()
    connect_started = threading.Event()
    allow_connect = threading.Event()

    def slow_connect() -> None:
        connect_started.set()
        allow_connect.wait(timeout=2.0)
        bridge._backend = backend

    bridge._connect = slow_connect
    bridge._running = True
    worker = threading.Thread(target=bridge._loop)
    bridge._t = worker
    worker.start()

    assert connect_started.wait(timeout=1.0)
    bridge.stop()
    assert worker.is_alive()

    allow_connect.set()
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert backend.closed.is_set()


def test_reconnect_requests_a_full_state_resend(monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = PebbleBridge(
        "00000000-0000-0000-0000-000000000000",
        mac="AA:BB:CC:DD:EE:FF",
    )
    bridge.update(hr=140, speed_mps=2.5, cadence=85)
    backend = _Backend()
    calls: list[bool] = []
    bridge._running = True

    def fake_connect() -> None:
        bridge._backend = backend

    def fake_send(*, full: bool) -> None:
        calls.append(full)
        PebbleBridge._send_once(bridge, full=full)
        bridge._running = False

    monkeypatch.setattr(bridge, "_connect", fake_connect)
    monkeypatch.setattr(bridge, "_send_once", fake_send)
    monkeypatch.setattr(pebble_module.time, "sleep", lambda _seconds: None)

    bridge._loop()

    assert calls == [True]
    assert set(_sent_values(backend)) == {
        pebble_module.KEY_HR,
        pebble_module.KEY_PACE,
        pebble_module.KEY_CADENCE,
    }


def test_watch_launch_requests_a_full_state_resend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge, backend = _bridge_with_backend()
    bridge.update(hr=140, units=1, workout_step=3)
    bridge._send_once()
    backend.messages.clear()

    bridge._on_app_message(
        bridge.app_uuid,
        {pebble_module.KEY_SYNC_REQUEST: 1},
    )
    bridge._running = True
    calls: list[bool] = []

    def send_requested_state(*, full: bool) -> None:
        calls.append(full)
        PebbleBridge._send_once(bridge, full=full)
        bridge._running = False

    monkeypatch.setattr(bridge, "_send_once", send_requested_state)
    bridge._loop()

    assert calls == [True]
    assert set(_sent_values(backend)) == {
        pebble_module.KEY_HR,
        pebble_module.KEY_UNITS,
        pebble_module.KEY_WORKOUT_STEP,
    }


def test_cobble_uint8_watch_launch_request_is_normalized() -> None:
    cobble_client = pytest.importorskip("cobble_client")
    bridge = PebbleBridge("00000000-0000-0000-0000-000000000000")
    backend = pebble_module._CobbleBackend.__new__(pebble_module._CobbleBackend)
    backend._message_callback = bridge._on_app_message

    backend._handle_app_message(
        bridge.app_uuid,
        {pebble_module.KEY_SYNC_REQUEST: cobble_client.u8(1)},
    )

    assert bridge._full_request_id == 1

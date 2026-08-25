"""XDG Location Portal adapter tests using an in-memory fake bus."""

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import Mock

import pytest
from dbus_fast import BusType, Variant
from fitness_tracker.hardware import location_portal
from fitness_tracker.hardware.location import (
    LocationFix,
    LocationPolicy,
    LocationState,
    PortalAccuracy,
)
from fitness_tracker.hardware.location_portal import (
    LOCATION_INTERFACE,
    PORTAL_PATH,
    PORTAL_SERVICE,
    REQUEST_INTERFACE,
    SESSION_INTERFACE,
    PortalLocationSource,
    _predicted_request_path,
)


class _FakeRequest:
    def __init__(self) -> None:
        self.handlers: list[Any] = []
        self.close_count = 0
        self.block_close = False

    def on_response(self, handler: Any) -> None:
        self.handlers.append(handler)

    def off_response(self, handler: Any) -> None:
        if handler in self.handlers:
            self.handlers.remove(handler)

    async def call_close(self) -> None:
        self.close_count += 1
        if self.block_close:
            await asyncio.Event().wait()

    def emit_response(self, response: int, results: dict[str, object] | None = None) -> None:
        for handler in tuple(self.handlers):
            handler(response, results or {})


class _FakeSession:
    def __init__(self) -> None:
        self.handlers: list[Any] = []
        self.close_count = 0
        self.block_close = False

    def on_closed(self, handler: Any) -> None:
        self.handlers.append(handler)

    def off_closed(self, handler: Any) -> None:
        if handler in self.handlers:
            self.handlers.remove(handler)

    async def call_close(self) -> None:
        self.close_count += 1
        if self.block_close:
            await asyncio.Event().wait()

    def emit_closed(self, details: dict[str, object] | None = None) -> None:
        for handler in tuple(self.handlers):
            handler(details or {})


class _FakeLocation:
    def __init__(self, bus: "_FakeBus") -> None:
        self.bus = bus
        self.handlers: list[Any] = []
        self.create_options: dict[str, Variant] | None = None
        self.start_options: dict[str, Variant] | None = None
        self.start_session_path: str | None = None
        self.location_subscribed_before_start = False
        self.subscribed_before_start = False
        self.session_path = "/org/freedesktop/portal/desktop/session/1_1/session"
        self.returned_request_path: str | None = None

    def on_location_updated(self, handler: Any) -> None:
        self.handlers.append(handler)

    def off_location_updated(self, handler: Any) -> None:
        if handler in self.handlers:
            self.handlers.remove(handler)

    async def call_create_session(self, options: dict[str, Variant]) -> str:
        self.create_options = options
        return self.session_path

    async def call_start(
        self,
        session_path: str,
        _parent_window: str,
        options: dict[str, Variant],
    ) -> str:
        self.start_session_path = session_path
        self.start_options = options
        self.location_subscribed_before_start = bool(self.handlers)
        token = options["handle_token"].value
        predicted_path = _predicted_request_path(self.bus.unique_name, token)
        self.subscribed_before_start = bool(self.bus.requests[predicted_path].handlers)
        if self.returned_request_path is None:
            self.returned_request_path = predicted_path
        return self.returned_request_path

    def emit_update(self, session_path: str, values: dict[str, object]) -> None:
        for handler in tuple(self.handlers):
            handler(session_path, values)


class _FakeProxy:
    def __init__(self, interface_name: str, interface: object) -> None:
        self.interface_name = interface_name
        self.interface = interface

    def get_interface(self, name: str) -> object:
        assert name == self.interface_name
        return self.interface


class _FakeBus:
    unique_name = ":1.42"

    def __init__(self) -> None:
        self.location = _FakeLocation(self)
        self.session = _FakeSession()
        self.requests: dict[str, _FakeRequest] = {}
        self.introspected: list[tuple[str, str]] = []
        self.disconnected = False

    async def introspect(self, service: str, path: str) -> object:
        self.introspected.append((service, path))
        return object()

    def get_proxy_object(self, service: str, path: str, _introspection: object) -> _FakeProxy:
        assert service == PORTAL_SERVICE
        if path == PORTAL_PATH:
            return _FakeProxy(LOCATION_INTERFACE, self.location)
        if path == self.location.session_path:
            return _FakeProxy(SESSION_INTERFACE, self.session)
        self.requests.setdefault(path, _FakeRequest())
        return _FakeProxy(REQUEST_INTERFACE, self.requests[path])

    def disconnect(self) -> None:
        self.disconnected = True


def _factory(bus: _FakeBus):
    async def connect() -> _FakeBus:
        return bus

    return connect


def _exercise_start(
    source: PortalLocationSource,
    policy: LocationPolicy,
    on_fix: Any,
    on_state: Any,
) -> None:
    asyncio.run(source.start(policy, on_fix, on_state))


def _state_callback(states: list[tuple[LocationState, str | None]]):
    def record(state: LocationState, detail: str | None) -> None:
        states.append((state, detail))

    return record


def test_portal_source_uses_session_bus_options_and_delivers_fixes(monkeypatch) -> None:
    bus = _FakeBus()
    states: list[tuple[LocationState, str | None]] = []
    fixes: list[LocationFix] = []
    source = PortalLocationSource(bus_factory=_factory(bus))
    trace_logger = Mock()
    monkeypatch.setattr(location_portal, "logger", trace_logger)

    _exercise_start(source, LocationPolicy.outdoor(), fixes.append, _state_callback(states))

    assert bus.introspected == [(PORTAL_SERVICE, PORTAL_PATH)]
    assert bus.location.create_options is not None
    assert bus.location.create_options["accuracy"].value == int(PortalAccuracy.EXACT)
    assert bus.location.create_options["time-threshold"].value == 1
    assert (
        bus.location.create_options["distance-threshold"].value
        == LocationPolicy.outdoor().distance_threshold_m
    )
    assert bus.location.start_options is not None
    assert bus.location.start_session_path == bus.location.session_path
    assert bus.location.location_subscribed_before_start
    assert bus.location.subscribed_before_start
    assert bus.location.create_options["session_handle_token"].signature == "s"
    assert bus.location.create_options["accuracy"].signature == "u"
    assert bus.location.create_options["time-threshold"].signature == "u"
    assert bus.location.create_options["distance-threshold"].signature == "u"
    assert bus.location.start_options["handle_token"].signature == "s"

    request_path = next(iter(bus.requests))
    bus.requests[request_path].emit_response(0)
    bus.location.emit_update(
        bus.location.session_path,
        {
            "Latitude": Variant("d", 39.7392),
            "Longitude": Variant("d", -104.9903),
            "Accuracy": Variant("d", 5.0),
            "Altitude": Variant("d", 1_600.0),
            "Speed": Variant("d", 2.5),
            "Heading": Variant("d", 90.0),
            "Timestamp": Variant("(tt)", (1_700_000_000, 123_456)),
        },
    )

    assert states == [
        (LocationState.STARTING, None),
        (LocationState.ACQUIRING, None),
        (LocationState.TRACKING, None),
    ]
    assert fixes == [
        LocationFix(
            latitude_deg=39.7392,
            longitude_deg=-104.9903,
            accuracy_m=5.0,
            altitude_m=1_600.0,
            speed_mps=2.5,
            heading_deg=90.0,
            source_time_utc=datetime(2023, 11, 14, 22, 13, 20, 123_456, tzinfo=UTC),
        ),
    ]
    trace_logger.bind.assert_any_call(
        data={
            "raw": {
                "Latitude": 39.7392,
                "Longitude": -104.9903,
                "Accuracy": 5.0,
                "Altitude": 1_600.0,
                "Speed": 2.5,
                "Heading": 90.0,
                "Timestamp": (1_700_000_000, 123_456),
            },
            "normalized": fixes[0],
        },
    )
    trace_logger.bind.return_value.trace.assert_any_call("Received location portal fix")

    asyncio.run(source.stop())
    assert states[-1] == (LocationState.STOPPED, None)
    assert bus.session.close_count == 1
    assert bus.requests[request_path].close_count == 1
    assert bus.disconnected

    asyncio.run(source.stop())
    assert states.count((LocationState.STOPPED, None)) == 1


@pytest.mark.parametrize(
    ("response", "expected_state"),
    [
        (1, LocationState.CANCELLED),
        (2, LocationState.ERROR),
    ],
)
def test_portal_source_maps_non_successful_start_responses(
    response: int,
    expected_state: LocationState,
) -> None:
    bus = _FakeBus()
    source = PortalLocationSource(bus_factory=_factory(bus))
    states: list[tuple[LocationState, str | None]] = []

    _exercise_start(
        source,
        LocationPolicy.outdoor(),
        lambda _fix: None,
        _state_callback(states),
    )
    request_path = next(iter(bus.requests))
    bus.requests[request_path].emit_response(response)

    assert states[-1][0] is expected_state
    asyncio.run(source.stop())


def test_portal_source_repairs_a_legacy_request_path_and_filters_updates() -> None:
    bus = _FakeBus()
    bus.location.returned_request_path = "/org/freedesktop/portal/desktop/request/legacy/request"
    source = PortalLocationSource(bus_factory=_factory(bus))
    states: list[tuple[LocationState, str | None]] = []
    fixes: list[LocationFix] = []

    _exercise_start(source, LocationPolicy.outdoor(), fixes.append, _state_callback(states))
    predicted_path, predicted_request = next(iter(bus.requests.items()))
    actual_request = bus.requests[bus.location.returned_request_path]
    assert predicted_request.handlers == []
    assert len(actual_request.handlers) == 1

    actual_request.emit_response(0)
    bus.location.emit_update("/wrong/session", {"Latitude": 1.0, "Longitude": 2.0})
    bus.location.emit_update(bus.location.session_path, {"Latitude": 1.0})
    bus.location.emit_update(
        bus.location.session_path,
        {
            "Latitude": 39.7,
            "Longitude": -105.0,
            "Accuracy": -1.0,
            "Speed": -1.0,
            "Heading": 360.0,
            "Timestamp": (1, 1_000_000),
        },
    )

    assert predicted_path != bus.location.returned_request_path
    assert fixes == [LocationFix(latitude_deg=39.7, longitude_deg=-105.0)]
    assert states[-2:] == [
        (LocationState.ERROR, "malformed location update"),
        (LocationState.TRACKING, None),
    ]
    asyncio.run(source.stop())


def test_portal_source_dampens_repeated_malformed_update_errors(monkeypatch) -> None:
    monkeypatch.setattr(location_portal, "_MALFORMED_UPDATE_ERROR_COOLDOWN_S", 60.0)
    bus = _FakeBus()
    source = PortalLocationSource(bus_factory=_factory(bus))
    states: list[tuple[LocationState, str | None]] = []
    fixes: list[LocationFix] = []

    _exercise_start(source, LocationPolicy.outdoor(), fixes.append, _state_callback(states))
    bus.location.emit_update(bus.location.session_path, {"Latitude": 1.0})
    bus.location.emit_update(
        bus.location.session_path,
        {"Latitude": 39.7392, "Longitude": -104.9903},
    )
    bus.location.emit_update(bus.location.session_path, {"Latitude": 1.0})

    assert states == [
        (LocationState.STARTING, None),
        (LocationState.ERROR, "malformed location update"),
        (LocationState.TRACKING, None),
    ]
    assert fixes == [LocationFix(latitude_deg=39.7392, longitude_deg=-104.9903)]
    asyncio.run(source.stop())


def test_portal_source_generates_unique_session_and_request_tokens() -> None:
    first_bus = _FakeBus()
    second_bus = _FakeBus()
    first_source = PortalLocationSource(bus_factory=_factory(first_bus))
    second_source = PortalLocationSource(bus_factory=_factory(second_bus))

    _exercise_start(
        first_source,
        LocationPolicy.outdoor(),
        lambda _fix: None,
        lambda *_state: None,
    )
    _exercise_start(
        second_source,
        LocationPolicy.outdoor(),
        lambda _fix: None,
        lambda *_state: None,
    )

    assert first_bus.location.create_options is not None
    assert second_bus.location.create_options is not None
    assert first_bus.location.start_options is not None
    assert second_bus.location.start_options is not None
    assert (
        first_bus.location.create_options["session_handle_token"].value
        != second_bus.location.create_options["session_handle_token"].value
    )
    assert (
        first_bus.location.start_options["handle_token"].value
        != second_bus.location.start_options["handle_token"].value
    )

    asyncio.run(first_source.stop())
    asyncio.run(second_source.stop())


def test_portal_source_start_failure_is_nonfatal_and_stop_is_idempotent() -> None:
    class BrokenBusFactory:
        async def __call__(self) -> Any:
            message = "portal unavailable"
            raise RuntimeError(message)

    source = PortalLocationSource(bus_factory=BrokenBusFactory())
    states: list[tuple[LocationState, str | None]] = []

    _exercise_start(
        source,
        LocationPolicy.outdoor(),
        lambda _fix: None,
        _state_callback(states),
    )
    asyncio.run(source.stop())
    asyncio.run(source.stop())

    assert states[0] == (LocationState.STARTING, None)
    assert states[1] == (LocationState.UNAVAILABLE, "portal unavailable")


def test_portal_source_stop_is_safe_after_partial_startup() -> None:
    class PartialBus(_FakeBus):
        async def introspect(self, service: str, path: str) -> object:
            self.introspected.append((service, path))
            message = "introspection failed"
            raise RuntimeError(message)

    bus = PartialBus()
    source = PortalLocationSource(bus_factory=_factory(bus))
    states: list[tuple[LocationState, str | None]] = []

    _exercise_start(
        source,
        LocationPolicy.outdoor(),
        lambda _fix: None,
        _state_callback(states),
    )
    asyncio.run(source.stop())

    assert bus.disconnected
    assert source._bus is None  # noqa: SLF001
    assert states[1][0] is LocationState.UNAVAILABLE


def test_portal_source_bounds_startup_and_reports_timeout(monkeypatch) -> None:
    monkeypatch.setattr(location_portal, "_STARTUP_TIMEOUT_S", 0.01)

    class HangingBusFactory:
        async def __call__(self) -> Any:
            await asyncio.Event().wait()
            message = "unreachable"
            raise AssertionError(message)

    source = PortalLocationSource(bus_factory=HangingBusFactory())
    states: list[tuple[LocationState, str | None]] = []

    _exercise_start(
        source,
        LocationPolicy.outdoor(),
        lambda _fix: None,
        _state_callback(states),
    )

    assert states == [
        (LocationState.STARTING, None),
        (LocationState.UNAVAILABLE, "Location portal startup timed out"),
    ]
    assert source._bus is None  # noqa: SLF001
    asyncio.run(source.stop())


def test_portal_source_allows_slow_activation_within_startup_budget(monkeypatch) -> None:
    monkeypatch.setattr(location_portal, "_STARTUP_TIMEOUT_S", 0.2)
    bus = _FakeBus()

    class SlowBusFactory:
        async def __call__(self) -> Any:
            await asyncio.sleep(0.02)
            return bus

    source = PortalLocationSource(bus_factory=SlowBusFactory())
    states: list[tuple[LocationState, str | None]] = []

    _exercise_start(
        source,
        LocationPolicy.outdoor(),
        lambda _fix: None,
        _state_callback(states),
    )

    assert states == [(LocationState.STARTING, None)]
    assert bus.introspected == [(PORTAL_SERVICE, PORTAL_PATH)]
    asyncio.run(source.stop())


def test_portal_source_ignores_callbacks_after_stop() -> None:
    bus = _FakeBus()
    source = PortalLocationSource(bus_factory=_factory(bus))
    states: list[tuple[LocationState, str | None]] = []
    fixes: list[LocationFix] = []

    _exercise_start(source, LocationPolicy.outdoor(), fixes.append, _state_callback(states))
    location_handler = bus.location.handlers[0]
    session_handler = bus.session.handlers[0]
    request_path = next(iter(bus.requests))
    request_handler = bus.requests[request_path].handlers[0]
    asyncio.run(source.stop())

    location_handler(
        bus.location.session_path,
        {"Latitude": 39.7392, "Longitude": -104.9903},
    )
    session_handler({})
    request_handler(0, {})

    assert fixes == []
    assert states == [(LocationState.STARTING, None), (LocationState.STOPPED, None)]


def test_connect_session_bus_uses_the_session_bus(monkeypatch) -> None:
    bus_types: list[BusType] = []

    class FakeMessageBus:
        def __init__(self, *, bus_type: BusType) -> None:
            bus_types.append(bus_type)

        async def connect(self) -> "FakeMessageBus":
            return self

    monkeypatch.setattr(location_portal, "MessageBus", FakeMessageBus)

    bus = asyncio.run(location_portal._connect_session_bus())  # noqa: SLF001

    assert bus_types == [BusType.SESSION]
    assert isinstance(bus, FakeMessageBus)


def test_portal_source_reports_unexpected_session_close() -> None:
    bus = _FakeBus()
    source = PortalLocationSource(bus_factory=_factory(bus))
    states: list[tuple[LocationState, str | None]] = []

    _exercise_start(
        source,
        LocationPolicy.outdoor(),
        lambda _fix: None,
        _state_callback(states),
    )
    bus.session.emit_closed({"reason": Variant("s", "provider-closed")})

    assert states[-1] == (LocationState.UNAVAILABLE, "location session closed")
    asyncio.run(source.stop())


def test_portal_source_bounds_cleanup_and_clears_references(monkeypatch) -> None:
    monkeypatch.setattr(location_portal, "_CLEANUP_OPERATION_TIMEOUT_S", 0.01)
    bus = _FakeBus()
    source = PortalLocationSource(bus_factory=_factory(bus))

    async def exercise() -> None:
        await source.start(LocationPolicy.outdoor(), lambda _fix: None, lambda *_state: None)
        request_path = next(iter(bus.requests))

        bus.requests[request_path].block_close = True
        bus.session.block_close = True
        await source.stop()

    asyncio.run(exercise())

    assert bus.disconnected
    assert source._bus is None  # noqa: SLF001
    assert source._session_interface is None  # noqa: SLF001
    assert source._request_interface is None  # noqa: SLF001


def test_portal_source_cleanup_continues_after_cancellation(monkeypatch) -> None:
    monkeypatch.setattr(location_portal, "_CLEANUP_OPERATION_TIMEOUT_S", 0.01)
    bus = _FakeBus()
    states: list[tuple[LocationState, str | None]] = []
    source = PortalLocationSource(bus_factory=_factory(bus))

    async def exercise() -> None:
        await source.start(LocationPolicy.outdoor(), lambda _fix: None, _state_callback(states))
        request_path = next(iter(bus.requests))

        bus.requests[request_path].block_close = True
        task = asyncio.create_task(source.stop())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert bus.disconnected
    assert source._bus is None  # noqa: SLF001
    assert source._session_interface is None  # noqa: SLF001
    assert source._request_interface is None  # noqa: SLF001
    assert states[-1] == (LocationState.STOPPED, None)

"""XDG Location Portal source implemented with dbus-fast."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from math import isfinite
from typing import Protocol, cast
from uuid import uuid4

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.introspection import Node
from dbus_fast.unpack import unpack_variants
from loguru import logger

from fitness_tracker.hardware.location import (
    LocationFix,
    LocationFixCallback,
    LocationPolicy,
    LocationSource,
    LocationState,
    LocationStateCallback,
)

PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
LOCATION_INTERFACE = "org.freedesktop.portal.Location"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"
SESSION_INTERFACE = "org.freedesktop.portal.Session"
_STARTUP_TIMEOUT_S = 10.0
_CLEANUP_OPERATION_TIMEOUT_S = 2.0
_MALFORMED_UPDATE_ERROR_COOLDOWN_S = 5.0

_REQUEST_INTROSPECTION = """
<node>
  <interface name="org.freedesktop.portal.Request">
    <method name="Close"/>
    <signal name="Response">
      <arg name="response" type="u" direction="out"/>
      <arg name="results" type="a{sv}" direction="out"/>
    </signal>
  </interface>
</node>
"""
_SESSION_INTROSPECTION = """
<node>
  <interface name="org.freedesktop.portal.Session">
    <method name="Close"/>
    <signal name="Closed">
      <arg name="details" type="a{sv}" direction="out"/>
    </signal>
  </interface>
</node>
"""
_REQUEST_NODE = Node.parse(_REQUEST_INTROSPECTION)
_SESSION_NODE = Node.parse(_SESSION_INTROSPECTION)
_PORTAL_TIMESTAMP_PARTS = 2
_MICROSECONDS_PER_SECOND = 1_000_000

BusFactory = Callable[[], Awaitable[MessageBus]]


class _LocationInterface(Protocol):
    def on_location_updated(self, handler: Callable[..., object]) -> None: ...

    def off_location_updated(self, handler: Callable[..., object]) -> None: ...

    async def call_create_session(self, options: Mapping[str, Variant]) -> str: ...

    async def call_start(
        self,
        session_path: str,
        parent_window: str,
        options: Mapping[str, Variant],
    ) -> str: ...


class _SessionInterface(Protocol):
    def on_closed(self, handler: Callable[..., object]) -> None: ...

    def off_closed(self, handler: Callable[..., object]) -> None: ...

    async def call_close(self) -> None: ...


class _RequestInterface(Protocol):
    def on_response(self, handler: Callable[..., object]) -> None: ...

    def off_response(self, handler: Callable[..., object]) -> None: ...

    async def call_close(self) -> None: ...


async def _connect_session_bus() -> MessageBus:
    """Connect to the user's session bus."""
    return await MessageBus(bus_type=BusType.SESSION).connect()


def _token() -> str:
    """Return a unique, valid portal object-path element."""
    return f"fitness_tracker_{uuid4().hex}"


def _predicted_request_path(unique_name: str | None, token: str) -> str:
    """Predict the request path used by current and older portal versions."""
    if not unique_name or not unique_name.startswith(":"):
        message = "session bus did not provide a unique name"
        raise RuntimeError(message)
    sender = unique_name[1:].replace(".", "_")
    return f"{PORTAL_PATH}/request/{sender}/{token}"


def _portal_timestamp(value: object) -> datetime | None:
    """Decode the portal's unsigned ``(seconds, microseconds)`` timestamp."""
    if not isinstance(value, tuple) or len(value) != _PORTAL_TIMESTAMP_PARTS:
        return None
    seconds, microseconds = value
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, int)
        or seconds < 0
        or isinstance(microseconds, bool)
        or not isinstance(microseconds, int)
        or not 0 <= microseconds < _MICROSECONDS_PER_SECOND
    ):
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=microseconds)
    except (OverflowError, OSError, ValueError):
        return None


class PortalLocationSource(LocationSource):
    """Acquire location fixes through the XDG Location Portal."""

    def __init__(
        self,
        *,
        bus_factory: BusFactory | None = None,
        parent_window: str = "",
    ) -> None:
        self._bus_factory = bus_factory or _connect_session_bus
        self._parent_window = parent_window
        self._bus: MessageBus | None = None
        self._location_interface: _LocationInterface | None = None
        self._session_interface: _SessionInterface | None = None
        self._request_interface: _RequestInterface | None = None
        self._session_path: str | None = None
        self._request_path: str | None = None
        self._location_handler: Callable[..., object] | None = None
        self._session_handler: Callable[..., object] | None = None
        self._request_handler: Callable[..., object] | None = None
        self._on_fix: LocationFixCallback | None = None
        self._on_state: LocationStateCallback | None = None
        self._last_malformed_update_error_at: float | None = None
        self._stopped = True

    async def start(
        self,
        policy: LocationPolicy,
        on_fix: LocationFixCallback,
        on_state: LocationStateCallback,
    ) -> None:
        """Start a portal session and begin location acquisition."""
        if self._bus is not None:
            message = "location source is already started"
            raise RuntimeError(message)

        self._stopped = False
        self._on_fix = on_fix
        self._on_state = on_state
        self._last_malformed_update_error_at = None
        self._report_state(LocationState.STARTING, None)

        try:
            await asyncio.wait_for(
                self._start_portal(policy),
                timeout=_STARTUP_TIMEOUT_S,
            )
        except asyncio.CancelledError:
            await self._cleanup()
            raise
        except TimeoutError:
            logger.warning("Location portal startup timed out")
            self._report_state(
                LocationState.UNAVAILABLE,
                "Location portal startup timed out",
            )
            await self._cleanup()
        except Exception as error:
            logger.warning("Location portal startup failed: {}", error)
            self._report_state(LocationState.UNAVAILABLE, str(error))
            await self._cleanup()

    async def _start_portal(self, policy: LocationPolicy) -> None:
        """Connect to the portal, create a session, and issue Start."""
        bus = await self._bus_factory()
        self._bus = bus
        node = await bus.introspect(PORTAL_SERVICE, PORTAL_PATH)
        portal_object = bus.get_proxy_object(PORTAL_SERVICE, PORTAL_PATH, node)
        location = cast("_LocationInterface", portal_object.get_interface(LOCATION_INTERFACE))
        self._location_interface = location
        self._location_handler = self._handle_location_updated
        location.on_location_updated(self._location_handler)

        session_path = await self._create_session(location, policy)
        self._install_session(session_path)
        await self._start_session(location, session_path)

    async def _create_session(self, location: _LocationInterface, policy: LocationPolicy) -> str:
        """Create the portal session with the requested policy options."""
        session_options = {
            "session_handle_token": Variant("s", _token()),
            "accuracy": Variant("u", int(policy.accuracy)),
            "time-threshold": Variant("u", policy.time_threshold_s),
            "distance-threshold": Variant("u", policy.distance_threshold_m),
        }
        session_path = await location.call_create_session(session_options)
        return _require_object_path(session_path, "location session")

    def _install_session(self, session_path: str) -> None:
        """Subscribe to lifecycle signals on the created session object."""
        bus = self._require_bus()
        self._session_path = session_path
        session_object = bus.get_proxy_object(PORTAL_SERVICE, session_path, _SESSION_NODE)
        session = cast("_SessionInterface", session_object.get_interface(SESSION_INTERFACE))
        self._session_interface = session
        self._session_handler = self._handle_session_closed
        session.on_closed(self._session_handler)

    async def _start_session(self, location: _LocationInterface, session_path: str) -> None:
        """Subscribe to and start the portal request."""
        bus = self._require_bus()
        request_token = _token()
        predicted_path = _predicted_request_path(bus.unique_name, request_token)
        request_object = bus.get_proxy_object(PORTAL_SERVICE, predicted_path, _REQUEST_NODE)
        request = cast("_RequestInterface", request_object.get_interface(REQUEST_INTERFACE))
        self._request_interface = request
        self._request_path = predicted_path
        self._request_handler = self._handle_start_response
        request.on_response(self._request_handler)

        returned_path = await location.call_start(
            session_path,
            self._parent_window,
            {"handle_token": Variant("s", request_token)},
        )
        returned_path = _require_object_path(returned_path, "start request")
        if returned_path != predicted_path:
            self._move_request_subscription(returned_path)

    async def stop(self) -> None:
        """Stop the portal request/session and disconnect the session bus."""
        was_stopped = self._stopped
        state_callback = self._on_state
        self._stopped = True
        try:
            await self._cleanup()
        finally:
            if not was_stopped and state_callback is not None:
                try:
                    state_callback(LocationState.STOPPED, None)
                except Exception:
                    logger.exception("Location state callback failed")

    def _move_request_subscription(self, request_path: str) -> None:
        """Move the response subscription when an older portal changes the handle."""
        request = self._request_interface
        handler = self._request_handler
        bus = self._require_bus()
        if request is None or handler is None:
            return
        self._remove_handler(request, "off_response", handler)
        request_object = bus.get_proxy_object(
            PORTAL_SERVICE,
            request_path,
            _REQUEST_NODE,
        )
        request = cast("_RequestInterface", request_object.get_interface(REQUEST_INTERFACE))
        request.on_response(handler)
        self._request_interface = request
        self._request_path = request_path

    def _handle_start_response(self, response: int, _results: Mapping[str, object]) -> None:
        """Map the portal request response to a nonfatal source state."""
        if self._stopped:
            return
        if response == 0:
            self._report_state(LocationState.ACQUIRING, None)
        elif response == 1:
            self._report_state(LocationState.CANCELLED, "location permission was cancelled")
        else:
            self._report_state(LocationState.ERROR, "location portal request failed")

    def _handle_session_closed(self, _details: Mapping[str, object] | None = None) -> None:
        """Report an unexpected portal session closure without raising."""
        if not self._stopped:
            self._report_state(LocationState.UNAVAILABLE, "location session closed")

    def _handle_location_updated(
        self,
        session_path: str,
        location: Mapping[str, object],
    ) -> None:
        """Convert one portal signal into a validated location fix."""
        if self._stopped or session_path != self._session_path:
            return
        try:
            values = _location_values(location)
            latitude = values.get("Latitude")
            longitude = values.get("Longitude")
            fix = LocationFix(
                latitude_deg=_required_float(latitude, "Latitude"),
                longitude_deg=_required_float(longitude, "Longitude"),
                accuracy_m=_optional_float(values.get("Accuracy")),
                altitude_m=_optional_float(values.get("Altitude")),
                speed_mps=_optional_float(values.get("Speed")),
                heading_deg=_optional_float(values.get("Heading")),
                source_time_utc=_portal_timestamp(values.get("Timestamp")),
            )
        except (TypeError, ValueError, OverflowError) as error:
            logger.warning("Ignoring malformed location update: {}", error)
            now = time.monotonic()
            last_reported = self._last_malformed_update_error_at
            if last_reported is None or now - last_reported >= _MALFORMED_UPDATE_ERROR_COOLDOWN_S:
                self._last_malformed_update_error_at = now
                self._report_state(LocationState.ERROR, "malformed location update")
            return

        self._report_state(LocationState.TRACKING, None)
        callback = self._on_fix
        if callback is None:
            return
        try:
            callback(fix)
        except Exception:
            logger.exception("Location fix callback failed")

    def _report_state(self, state: LocationState, detail: str | None) -> None:
        callback = self._on_state
        if callback is None:
            return
        try:
            callback(state, detail)
        except Exception:
            logger.exception("Location state callback failed")

    @staticmethod
    def _remove_handler(
        interface: object,
        method_name: str,
        handler: Callable[..., object],
    ) -> None:
        try:
            getattr(interface, method_name)(handler)
        except Exception:
            logger.debug("Location portal signal handler removal failed")

    async def _cleanup(self) -> None:
        """Remove handlers, bound portal shutdown calls, and clear all references."""
        location = self._location_interface
        session = self._session_interface
        request = self._request_interface
        bus = self._bus
        cancelled = False
        if location is not None and self._location_handler is not None:
            self._remove_handler(location, "off_location_updated", self._location_handler)
        if session is not None and self._session_handler is not None:
            self._remove_handler(session, "off_closed", self._session_handler)
        if request is not None and self._request_handler is not None:
            self._remove_handler(request, "off_response", self._request_handler)

        if request is not None:
            cancelled |= await self._bounded_cleanup_call(request.call_close, "request close")
        if session is not None:
            cancelled |= await self._bounded_cleanup_call(session.call_close, "session close")

        try:
            if bus is not None:
                cancelled |= await self._bounded_cleanup_call(bus.disconnect, "bus disconnect")
        finally:
            self._bus = None
            self._location_interface = None
            self._session_interface = None
            self._request_interface = None
            self._session_path = None
            self._request_path = None
            self._location_handler = None
            self._session_handler = None
            self._request_handler = None
            self._on_fix = None
            self._on_state = None

        if cancelled:
            raise asyncio.CancelledError

    @staticmethod
    async def _bounded_cleanup_call(
        operation: Callable[[], Awaitable[object] | object],
        name: str,
    ) -> bool:
        """Run one cleanup operation with a timeout and retain cancellation state."""
        try:
            result = operation()
            if inspect.isawaitable(result):
                await asyncio.wait_for(
                    result,
                    timeout=_CLEANUP_OPERATION_TIMEOUT_S,
                )
        except TimeoutError:
            logger.warning("Location portal {} timed out", name)
        except asyncio.CancelledError:
            logger.debug("Location portal {} cancelled", name)
            return True
        except Exception as error:
            logger.debug("Location portal {} failed: {}", name, error)
        return False

    def _require_bus(self) -> MessageBus:
        if self._bus is None:
            message = "location portal bus is not connected"
            raise RuntimeError(message)
        return self._bus


def _require_object_path(value: object, name: str) -> str:
    """Validate a portal object path returned by a method call."""
    if not isinstance(value, str) or not value.startswith("/"):
        message = f"portal returned an invalid {name} handle"
        raise TypeError(message)
    return value


def _location_values(value: object) -> dict[str, object]:
    """Unpack and validate a portal location vardict."""
    values = unpack_variants(value)
    if not isinstance(values, dict):
        message = "location update is not a dictionary"
        raise TypeError(message)
    latitude = values.get("Latitude")
    longitude = values.get("Longitude")
    if latitude is None or longitude is None:
        message = "location update is missing latitude or longitude"
        raise ValueError(message)
    return values


def _required_float(value: object, name: str) -> float:
    """Validate a required finite D-Bus double."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        message = f"location {name} is not numeric"
        raise TypeError(message)
    number = float(value)
    if not isfinite(number):
        message = f"location {name} is not finite"
        raise ValueError(message)
    return number


def _optional_float(value: object) -> float | None:
    """Return a finite float for an optional portal value."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not isfinite(number):
        return None
    return number

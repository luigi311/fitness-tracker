"""Shared metric display widget for session views."""

from __future__ import annotations

from typing import TYPE_CHECKING

import gi

from fitness_tracker.core.sensor_status import SensorKind

if TYPE_CHECKING:
    from fitness_tracker.core.sensor_status import SensorStatus

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango  # noqa: E402  # ty:ignore[unresolved-import]


class MetricTile(Gtk.Frame):
    """Display a metric value, label, unit, and connection status."""

    def __init__(
        self,
        title: str,
        unit: str | None = None,
        *,
        sensor: SensorKind | None = None,
    ) -> None:
        """Build a live metric value, caption, unit, and source-status indicator."""
        super().__init__()
        self.sensor = sensor
        self._connected = False
        self._last_text = "—"
        self.set_hexpand(True)
        self.set_size_request(100, -1)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        inner.set_margin_top(4)
        inner.set_margin_bottom(4)
        inner.set_margin_start(8)
        inner.set_margin_end(8)
        inner.set_valign(Gtk.Align.CENTER)

        self.label = Gtk.Label(label=title, xalign=0.0)
        self.label.add_css_class("caption")
        self.label.set_ellipsize(Pango.EllipsizeMode.END)
        self.label.set_max_width_chars(12)
        self.label.set_hexpand(True)

        self.value = Gtk.Label(label="—")
        self.value.add_css_class("metric-value")
        self.value.add_css_class("numeric")
        self.value.set_xalign(0.0)
        self.value.set_ellipsize(Pango.EllipsizeMode.END)
        self.value.set_max_width_chars(7)

        self.unit = Gtk.Label(label=unit or "", xalign=0.0)
        self.unit.add_css_class("dim-label")
        self.unit.add_css_class("caption")
        self.unit.set_valign(Gtk.Align.CENTER)
        self.unit.set_visible(bool(unit))
        details = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        details.append(self.label)
        details.append(self.unit)
        inner.append(details)
        inner.append(self.value)

        self.dot = Gtk.Label(label="○ Offline", xalign=0.0)
        self.dot.add_css_class("metric-status")

        inner.append(self.dot)
        self.set_child(inner)

    def set_value(self, text: str) -> None:
        """Update the displayed metric value."""
        self._last_text = text
        self.value.set_text(text if self._connected or self.sensor is SensorKind.DISTANCE else "—")

    def set_unit(self, text: str) -> None:
        """Update and show or hide the metric unit."""
        self.unit.set_text(text)
        self.unit.set_visible(bool(text))

    def set_status(self, *, connected: bool, tooltip: str | None = None) -> None:
        """Update the connection indicator and disconnected-value styling."""
        self._connected = connected
        self.set_value(self._last_text)
        self.dot.set_text("● Live" if connected else "○ Offline")
        if connected:
            self.dot.add_css_class("metric-live")
        else:
            self.dot.remove_css_class("metric-live")
        self.dot.set_tooltip_text(tooltip or None)
        self.set_tooltip_text(tooltip or None)
        self.dot.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [tooltip or ("Sensor connected" if connected else "Sensor not connected")],
        )
        opacity = 1.0 if connected else 0.55
        self.value.set_opacity(opacity)
        self.unit.set_opacity(opacity)

    def apply_status(self, status: SensorStatus) -> None:
        """Apply the connection status for this tile's bound sensor."""
        sensor = self.sensor
        if sensor is None:
            return
        connected = status.is_connected(sensor)
        self.set_status(
            connected=connected,
            tooltip=sensor.tooltip(connected=connected),
        )

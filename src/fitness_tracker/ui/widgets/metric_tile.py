"""Shared metric display widget for session views."""

from __future__ import annotations

from typing import TYPE_CHECKING

import gi

if TYPE_CHECKING:
    from fitness_tracker.core.sensor_status import SensorKind, SensorStatus

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402  # ty:ignore[unresolved-import]


class MetricTile(Gtk.Frame):
    """Display a metric value, label, unit, and connection status."""

    def __init__(
        self,
        title: str,
        unit: str | None = None,
        *,
        sensor: SensorKind | None = None,
    ) -> None:
        super().__init__()
        self.sensor = sensor
        self.set_hexpand(True)
        self.set_size_request(120, -1)

        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        inner.set_margin_top(4)
        inner.set_margin_bottom(4)
        inner.set_margin_start(4)
        inner.set_margin_end(4)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.value = Gtk.Label(label="0")
        self.value.add_css_class("title-1")
        self.value.add_css_class("numeric")
        self.value.set_xalign(0.0)

        details = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.label = Gtk.Label(label=title, xalign=0.0)
        self.label.add_css_class("dim-label")
        self.unit = Gtk.Label(label=unit or "", xalign=0.0)
        self.unit.add_css_class("dim-label")
        self.unit.set_visible(bool(unit))
        details.append(self.label)
        details.append(self.unit)

        content.append(self.value)
        content.append(details)

        self.dot = Gtk.Label(label="⚫")
        self.dot.set_xalign(1.0)

        inner.append(content)
        inner.append(Gtk.Box(hexpand=True))
        inner.append(self.dot)
        self.set_child(inner)

    def set_value(self, text: str) -> None:
        """Update the displayed metric value."""
        self.value.set_text(text)

    def set_unit(self, text: str) -> None:
        """Update and show or hide the metric unit."""
        self.unit.set_text(text)
        self.unit.set_visible(bool(text))

    def set_status(self, *, connected: bool, tooltip: str | None = None) -> None:
        """Update the connection indicator and disconnected-value styling."""
        self.dot.set_text("🟢" if connected else "⚫")
        self.dot.set_tooltip_text(tooltip or None)
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

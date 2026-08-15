"""Shared session timer widget."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402  # ty:ignore[unresolved-import]


class SessionTimer(Gtk.Frame):
    """Display a session timer with an optional caption."""

    def __init__(self, caption: str | None = None) -> None:
        super().__init__()
        self.set_hexpand(True)

        if caption is None:
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            box.set_margin_top(16)
            box.set_margin_bottom(16)
            box.set_margin_start(4)
            box.set_margin_end(4)
            box.set_halign(Gtk.Align.CENTER)

            self.value = Gtk.Label(label="00:00:00")
            self.value.add_css_class("title-1")
            self.value.set_halign(Gtk.Align.CENTER)
            self.value.set_xalign(0.5)
            box.append(self.value)
        else:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            box.set_margin_top(8)
            box.set_margin_bottom(8)
            box.set_margin_start(4)
            box.set_margin_end(4)

            label = Gtk.Label(label=caption, xalign=0.0)
            label.add_css_class("dim-label")
            self.value = Gtk.Label(label="00:00", xalign=0.0)
            self.value.add_css_class("title-1")
            self.value.add_css_class("numeric")
            box.append(label)
            box.append(self.value)

        self.set_child(box)

    def set_text(self, text: str) -> None:
        """Update the displayed time."""
        self.value.set_text(text)

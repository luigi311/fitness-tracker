"""Scale the complete GTK widget tree after selecting its aspect-only layout."""

from __future__ import annotations

from functools import cache
from math import ceil, isclose
from typing import ClassVar

import gi

from fitness_tracker.core.layout import fit_layout

gi.require_versions({"Gtk": "4.0", "Gsk": "4.0"})
from gi.repository import Gdk, GLib, GObject, Gsk, Gtk  # noqa: E402  # ty:ignore[unresolved-import]


@cache
def _ensure_popup_css() -> None:
    """Move popup padding inside the transform instead of native-surface chrome."""
    provider = Gtk.CssProvider()
    provider.load_from_data(b"""
        popover.consistent-popup { padding: 0; margin: 0; }
        popover.consistent-popup > contents { padding: 12px; }
    """)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )


class _PopupLayout(Gtk.LayoutManager):
    """Measure native popup contents at the same uniform scale as the window."""

    def __init__(self, widget: Gtk.Popover, scale: float) -> None:
        """Keep native popup structure intact for GTK-owned dropdowns and menus."""
        super().__init__()
        self.scale = scale
        self.max_width: int | None = None
        # Capture native shadow bounds before replacing GTK's layout. Dropdowns
        # can preset a size request; exclude that request from the measurement.
        requested_size = widget.get_size_request()
        widget.set_size_request(-1, -1)
        try:
            child = widget.get_first_child()
            self._chrome = {
                orientation: max(
                    0,
                    Gtk.Popover.do_measure(widget, orientation, -1)[0]
                    - child.measure(orientation, -1)[0],
                )
                for orientation in (Gtk.Orientation.HORIZONTAL, Gtk.Orientation.VERTICAL)
            }
        finally:
            widget.set_size_request(*requested_size)

    def set_scale(self, scale: float) -> None:
        """Remeasure a native popup when its parent interface changes zoom."""
        if not isclose(scale, self.scale):
            self.scale = scale
            self.layout_changed()

    def do_measure(
        self,
        widget: Gtk.Popover,
        orientation: Gtk.Orientation,
        for_size: int,
    ) -> tuple[int, int, int, int]:
        """Expose scaled content minima to GTK's native popup sizing."""
        child = widget.get_first_child()
        chrome = self.chrome_size(orientation)
        if for_size >= 0:
            other = (
                Gtk.Orientation.VERTICAL
                if orientation is Gtk.Orientation.HORIZONTAL
                else Gtk.Orientation.HORIZONTAL
            )
            for_size = max(0, round((for_size - self.chrome_size(other)) / self.scale))
        minimum, natural, _min_base, _nat_base = child.measure(orientation, for_size)
        minimum = chrome + ceil(minimum * self.scale)
        natural = chrome + ceil(natural * self.scale)
        if orientation is Gtk.Orientation.HORIZONTAL and self.max_width is not None:
            minimum = min(minimum, self.max_width)
            natural = min(natural, self.max_width)
        return minimum, natural, -1, -1

    def chrome_size(self, orientation: Gtk.Orientation) -> int:
        """Retain the native chrome measured before replacing GTK's layout."""
        return self._chrome[orientation]

    def do_allocate(
        self,
        widget: Gtk.Popover,
        width: int,
        height: int,
        _baseline: int,
    ) -> None:
        """Transform the popup contents inside their own native surface."""
        child = widget.get_first_child()
        child_width, child_height = round(width / self.scale), round(height / self.scale)
        child.measure(Gtk.Orientation.HORIZONTAL, -1)
        child.measure(Gtk.Orientation.VERTICAL, child_width)
        child.allocate(
            child_width,
            child_height,
            -1,
            Gsk.Transform.new().scale(self.scale, self.scale),
        )


class _AspectLayout(Gtk.LayoutManager):
    """Allocate one reference-size child with a uniform GTK transform."""

    def __init__(self) -> None:
        """Wait for the first real allocation before applying aspect hysteresis."""
        super().__init__()
        self._allocated = False

    def do_measure(
        self,
        _widget: Gtk.Widget,
        _orientation: Gtk.Orientation,
        _for_size: int,
    ) -> tuple[int, int, int, int]:
        """Let the window size remain independent of the reference child's minimum."""
        return 1, 1, -1, -1

    def do_allocate(
        self,
        widget: ScaledView,
        width: int,
        height: int,
        _baseline: int,
    ) -> None:
        """Select placement before allocating and transforming the existing child."""
        previous = widget.layout_frame
        frame = fit_layout(
            max(1, width),
            max(1, height),
            previous.family if self._allocated else None,
        )
        self._allocated = True
        widget.layout_frame = frame
        if frame != previous:
            widget.emit("layout-changed")
        widget.queue_popup_sync()
        child = widget.get_first_child()
        if child is not None:
            child.measure(Gtk.Orientation.HORIZONTAL, -1)
            child.measure(Gtk.Orientation.VERTICAL, frame.width)
            child.allocate(
                frame.width,
                frame.height,
                -1,
                Gsk.Transform.new().scale(frame.scale, frame.scale),
            )


class ScaledView(Gtk.Box):
    """Keep typography, controls, spacing, and hit testing under one transform."""

    __gsignals__: ClassVar = {"layout-changed": (GObject.SignalFlags.RUN_LAST, None, ())}

    def __init__(self, child: Gtk.Widget) -> None:
        """Own one persistent interface and let GTK manage its disposal normally."""
        super().__init__()
        _ensure_popup_css()
        self.layout_frame = fit_layout(360, 720)
        self._popup_source_id: int | None = None
        self.set_layout_manager(_AspectLayout())
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.append(child)
        self.connect("map", self.queue_popup_sync)
        self.connect("unmap", self._on_unmap)

    def queue_popup_sync(self, *_args: object) -> None:
        """Discover app-owned popup surfaces after an allocation or initial map."""
        if self._popup_source_id is None:
            self._popup_source_id = GLib.idle_add(self._sync_popups)

    def _on_unmap(self, *_args: object) -> None:
        """Cancel queued popup work when this interface leaves the screen."""
        if self._popup_source_id is not None:
            GLib.source_remove(self._popup_source_id)
            self._popup_source_id = None

    def _sync_popups(self) -> bool:
        """Scale native popovers without replacing GTK-owned child hierarchies."""
        self._popup_source_id = None
        if not self.get_mapped():
            return GLib.SOURCE_REMOVE
        pending = list(self)
        while pending:
            widget = pending.pop()
            if isinstance(widget, Gtk.Popover):
                manager = widget.get_layout_manager()
                if not isinstance(manager, _PopupLayout):
                    widget.set_has_arrow(False)
                    widget.add_css_class("consistent-popup")
                    manager = _PopupLayout(widget, self.layout_frame.scale)
                    widget.set_layout_manager(manager)
                    widget.connect("map", self.queue_popup_sync)
                manager.set_scale(self.layout_frame.scale)
                self._prepare_popup(widget)
                continue
            if widget.get_mapped():
                pending.extend(widget)
        return GLib.SOURCE_REMOVE

    def _prepare_popup(self, popup: Gtk.Popover) -> None:
        """Prepare anchor width and native bounds before a popup can be presented."""
        manager = popup.get_layout_manager()
        if not isinstance(manager, _PopupLayout):
            return
        manager.set_scale(self.layout_frame.scale)
        anchor = popup.get_parent()
        while anchor is not None and not isinstance(
            anchor,
            (Gtk.MenuButton, Gtk.DropDown, Gtk.ComboBox),
        ):
            anchor = anchor.get_parent()
        chrome = manager.chrome_size(Gtk.Orientation.HORIZONTAL)
        width = (
            round(anchor.get_width() * self.layout_frame.scale) + chrome
            if anchor is not None
            else -1
        )
        if anchor is not None and popup.has_css_class("content-width-popup"):
            maximum = max(
                chrome + 1,
                round((self.layout_frame.width - 24) * self.layout_frame.scale),
            )
            if manager.max_width != maximum:
                manager.max_width = maximum
                manager.layout_changed()
            width = -1
            success, bounds = self.compute_bounds(anchor)
            if success:
                pointing = Gdk.Rectangle()
                pointing.x = round(bounds.get_x() + bounds.get_width() / 2)
                pointing.y = 0
                pointing.width = 1
                pointing.height = max(1, anchor.get_height())
                popup.set_pointing_to(pointing)
        popup.set_size_request(width, -1)

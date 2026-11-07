"""Adaptive history presentation and compact, accessible choice controls."""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, ClassVar

import gi

from fitness_tracker.core.layout import LayoutFamily
from fitness_tracker.ui.layout import layout_frame, layout_owner

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Adw, Gdk, GLib, GObject, Gtk, Pango  # noqa: E402  # ty:ignore[unresolved-import]

if TYPE_CHECKING:
    from fitness_tracker.ui.widgets.chart import CompareChart

_CSS = b"""
.history-body button, .history-body dropdown, .history-body checkbutton { min-height: 48px; }
.history-body button { padding: 0 8px; }
.history-body .history-summary-value { font-size: 1.35em; font-weight: 700; }
.history-body .history-sparkline { min-height: 44px; }
.history-body flowboxchild { padding: 2px; }
"""


@cache
def _ensure_css() -> None:
    """Install the adaptive history styles once for the current display."""
    provider = Gtk.CssProvider()
    provider.load_from_data(_CSS)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )


class HistoryChoice(Gtk.DropDown):
    """Retain stable choice IDs while letting the selected label ellipsize."""

    __gsignals__: ClassVar = {"changed": (GObject.SignalFlags.RUN_LAST, None, ())}

    def __init__(self) -> None:
        """Create a choice control with stable IDs and separate popup labels."""
        super().__init__(model=Gtk.StringList.new([]))
        self._ids: list[str] = []
        self.set_hexpand(True)
        self.set_factory(self._factory(popup=False))
        self.set_list_factory(self._factory(popup=True))
        self.connect("notify::selected", lambda *_: self.emit("changed"))

    @staticmethod
    def _factory(*, popup: bool) -> Gtk.SignalListItemFactory:
        """Build label factories for compact selections or wrapped popup entries."""
        factory = Gtk.SignalListItemFactory()

        def setup(_factory: object, item: Gtk.ListItem) -> None:
            """Create the label used to display one choice."""
            label = Gtk.Label(xalign=0)
            label.set_max_width_chars(28 if popup else 18)
            if popup:
                label.set_wrap(True)
                label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            else:
                label.set_ellipsize(Pango.EllipsizeMode.END)
            item.set_child(label)

        def bind(_factory: object, item: Gtk.ListItem) -> None:
            """Populate a choice label from its current string item."""
            item.get_child().set_text(item.get_item().get_string())

        factory.connect("setup", setup)
        factory.connect("bind", bind)
        return factory

    def append(self, choice_id: str, label: str) -> None:
        """Append one named choice."""
        self._ids.append(choice_id)
        self.get_model().append(label)

    def remove_all(self) -> None:
        """Clear choices before rebuilding unit-dependent labels."""
        self._ids.clear()
        self.get_model().splice(0, self.get_model().get_n_items(), [])

    def get_active_id(self) -> str | None:
        """Return the stable ID of the selected option."""
        selected = self.get_selected()
        return self._ids[selected] if selected < len(self._ids) else None

    def set_active_id(self, choice_id: str) -> None:
        """Select a named option without depending on its display label."""
        self.set_selected(self._ids.index(choice_id))


class HistoryLayout(Gtk.Box):
    """Keep history widgets alive while applying the window's aspect arrangement."""

    def __init__(
        self,
        *,
        sidebar: Gtk.Box,
        activities: Gtk.ListBox,
        summary_metrics: Gtk.FlowBox,
        compare_controls: Gtk.Box,
        compare_frame: Gtk.Frame,
        legend: Gtk.ScrolledWindow,
        legend_menu: Gtk.MenuButton,
        chart: CompareChart,
    ) -> None:
        """Arrange persistent history widgets and install adaptive layout rules."""
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        _ensure_css()
        self.sidebar, self.activities, self.summary_metrics = sidebar, activities, summary_metrics
        self.compare_controls, self.legend, self.legend_menu = compare_controls, legend, legend_menu
        self.chart = chart
        self._landscape = False
        self.density = 1.0
        self._adaptive = Adw.BreakpointBin()
        self._adaptive.set_size_request(200, 160)
        self._adaptive.set_vexpand(True)
        self.append(self._adaptive)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.body.add_css_class("history-body")
        for side in ("top", "bottom", "start", "end"):
            getattr(self.body, f"set_margin_{side}")(8)
        self._adaptive.set_child(self.body)
        self.stack = Adw.ViewStack()
        self.stack.set_hhomogeneous(False)
        self.stack.set_vhomogeneous(False)
        self.stack.set_vexpand(True)
        self._build_modes()
        self.body.append(self.stack)
        self._build_activities()
        self._build_comparison(compare_frame)
        self._build_breakpoints()
        self.stack.connect("notify::visible-child-name", self._on_page_changed)
        self._layout_owner = None
        self._layout_handler_id = None
        self._settings_handler_id = None
        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)
        self._on_page_changed()

    def _on_map(self, *_args: object) -> None:
        """Subscribe to the shared layout only while the page is visible."""
        self._layout_owner = layout_owner(self)
        if self._layout_owner is not None:
            self._layout_handler_id = self._layout_owner.connect(
                "layout-changed",
                self._on_layout_changed,
            )
        self._settings_handler_id = self.get_settings().connect_object(
            "notify::gtk-xft-dpi",
            HistoryLayout._on_layout_changed,
            self,
        )
        self._on_layout_changed()

    def _on_unmap(self, *_args: object) -> None:
        """Release layout and font callbacks when leaving History."""
        if self._layout_handler_id is not None:
            self._layout_owner.disconnect(self._layout_handler_id)
            self._layout_handler_id = None
        self._layout_owner = None
        if self._settings_handler_id is not None:
            self.get_settings().disconnect(self._settings_handler_id)
            self._settings_handler_id = None

    def _build_modes(self) -> None:
        """Add linked controls for switching between activities and comparison."""
        modes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        modes.add_css_class("linked")
        modes.set_halign(Gtk.Align.CENTER)
        self.btn_activities = Gtk.ToggleButton(label="Activities")
        self.btn_compare = Gtk.ToggleButton(label="Compare")
        self.btn_compare.set_group(self.btn_activities)
        for button, name in ((self.btn_activities, "activities"), (self.btn_compare, "compare")):
            button.connect(
                "toggled",
                lambda current, page=name: (
                    self.stack.set_visible_child_name(page) if current.get_active() else None
                ),
            )
            modes.append(button)
        self.body.append(modes)

    def _build_activities(self) -> None:
        """Prepare shared or independent scrollers for the activity list."""
        self.activity_panes = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.activity_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.activity_content.append(self.sidebar)
        self.activity_content.append(self.activities)
        self.activity_scroll = self._scroller(self.activity_content)
        self.activity_panes.append(self.activity_scroll)
        self.list_scroll = self._scroller(None)
        self.list_scroll.set_visible(False)
        self.activity_panes.append(self.list_scroll)
        self.stack.add_titled(self.activity_panes, "activities", "Activities")
        for adjustment in (
            self.activity_scroll.get_hadjustment(),
            self.list_scroll.get_hadjustment(),
        ):
            adjustment.connect("changed", self._on_width_changed)

    def _build_comparison(self, frame: Gtk.Frame) -> None:
        """Place comparison controls and the chart in adaptive panes."""
        self.compare_panes = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.compare_panes.append(self.compare_controls)
        self.compare_panes.append(frame)
        self.compare_scroll = self._scroller(self.compare_panes)
        self.stack.add_titled(self.compare_scroll, "compare", "Compare")

    @staticmethod
    def _scroller(child: Gtk.Widget | None) -> Gtk.ScrolledWindow:
        """Create a vertically scrolling container that can shrink horizontally."""
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        scroller.set_child(child)
        return scroller

    def _build_breakpoints(self) -> None:
        """Contain page minima while the shared window aspect controls its panes."""
        self._adaptive.add_breakpoint(
            Adw.Breakpoint.new(Adw.BreakpointCondition.parse("min-width: 0px")),
        )

    def _on_page_changed(self, *_args: object) -> None:
        """Keep the mode buttons synchronized with the visible history page."""
        comparing = self.stack.get_visible_child_name() == "compare"
        (self.btn_compare if comparing else self.btn_activities).set_active(True)

    def _on_width_changed(self, *_args: object) -> None:
        """Keep summary columns stable within each aspect-defined arrangement."""
        frame = layout_frame(self)
        columns = 4 if frame.family is LayoutFamily.SQUARE and not self._landscape else 2
        self.summary_metrics.set_min_children_per_line(columns)
        self.summary_metrics.set_max_children_per_line(columns)

    def _on_layout_changed(self, *_args: object) -> None:
        """Keep filters and chart controls in an aspect-defined arrangement."""
        frame = layout_frame(self)
        landscape = frame.family is LayoutFamily.LANDSCAPE or (
            frame.family is LayoutFamily.SQUARE and frame.width >= frame.height
        )
        if landscape != self._landscape:
            self._move_activity_list(landscape=landscape)
        self._landscape = landscape
        self.activity_panes.set_orientation(
            Gtk.Orientation.HORIZONTAL if landscape else Gtk.Orientation.VERTICAL,
        )
        self.activity_scroll.set_size_request(210 if landscape else -1, -1)
        self.activity_scroll.set_hexpand(not landscape)
        filters = self.sidebar.get_first_child()
        filters.set_min_children_per_line(1 if landscape else 2)
        filters.set_max_children_per_line(1 if landscape else 2)
        self.compare_panes.set_orientation(
            Gtk.Orientation.HORIZONTAL if landscape else Gtk.Orientation.VERTICAL,
        )
        self.compare_controls.set_size_request(210 if landscape else -1, -1)
        self.compare_controls.set_hexpand(not landscape)
        self.legend.set_visible(landscape)
        self.legend.set_size_request(-1, 64)
        self.legend_menu.set_visible(not landscape)
        self.chart.canvas.set_size_request(140, 160)
        self.chart.set_display_scale(self.chart_display_scale)
        self._on_width_changed()

    @property
    def chart_display_scale(self) -> float:
        """Combine the layout density with GTK's current text scaling."""
        dpi = self.get_settings().get_property("gtk-xft-dpi")
        return self.density * (dpi / (96 * 1024) if dpi > 0 else 1)

    def _move_activity_list(self, *, landscape: bool) -> None:
        """Reparent the existing list while preserving its approximate scroll position."""
        old = self.list_scroll if self._landscape else self.activity_scroll
        offset = (
            0
            if self._landscape
            else self.sidebar.get_height() + self.activity_content.get_spacing()
        )
        position = max(0, old.get_vadjustment().get_value() - offset)
        if landscape:
            self.activity_content.remove(self.activities)
            self.list_scroll.set_child(self.activities)
        else:
            self.list_scroll.set_child(None)
            self.activity_content.append(self.activities)
        self.list_scroll.set_visible(landscape)

        def restore() -> bool:
            """Restore the list position after its new container is allocated."""
            target = self.list_scroll if landscape else self.activity_scroll
            offset = (
                self.sidebar.get_height() + self.activity_content.get_spacing()
                if not landscape and position
                else 0
            )
            target.get_vadjustment().set_value(position + offset)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(50, restore)

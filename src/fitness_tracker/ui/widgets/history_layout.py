"""Adaptive history presentation and compact, accessible choice controls."""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, ClassVar

import gi

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
.history-body.roomy { font-size: 2em; }
.history-body.roomy button, .history-body.roomy dropdown,
.history-body.roomy checkbutton { min-height: 96px; }
.history-body.roomy button image { -gtk-icon-size: 32px; }
.history-body.roomy .history-sparkline { min-height: 88px; }
.history-body.reduced { font-size: 0.8em; }
.history-body.reduced button, .history-body.reduced dropdown,
.history-body.reduced checkbutton { min-height: 40px; }
.history-body.reduced button image { -gtk-icon-size: 13px; }
.history-body.reduced .history-sparkline { min-height: 35px; }
.history-body.mini { font-size: 0.666667em; }
.history-body.mini button, .history-body.mini dropdown,
.history-body.mini checkbutton { min-height: 32px; }
.history-body.mini button image { -gtk-icon-size: 11px; }
.history-body.mini .history-sparkline { min-height: 29px; }
"""


@cache
def _ensure_css() -> None:
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
        super().__init__(model=Gtk.StringList.new([]))
        self._ids: list[str] = []
        self.set_hexpand(True)
        self.set_factory(self._factory(popup=False))
        self.set_list_factory(self._factory(popup=True))
        self.connect("notify::selected", lambda *_: self.emit("changed"))

    @staticmethod
    def _factory(*, popup: bool) -> Gtk.SignalListItemFactory:
        factory = Gtk.SignalListItemFactory()

        def setup(_factory: object, item: Gtk.ListItem) -> None:
            label = Gtk.Label(xalign=0)
            label.set_max_width_chars(28 if popup else 18)
            if popup:
                label.set_wrap(True)
                label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            else:
                label.set_ellipsize(Pango.EllipsizeMode.END)
            item.set_child(label)

        def bind(_factory: object, item: Gtk.ListItem) -> None:
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
    """Keep history widgets alive while changing density and panel placement."""

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
        self.connect("map", self._on_layout_changed)
        self._on_page_changed()
        self.get_settings().connect_object(
            "notify::gtk-xft-dpi",
            HistoryLayout._on_layout_changed,
            self,
        )

    def _build_modes(self) -> None:
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
        self.compare_panes = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.compare_panes.append(self.compare_controls)
        self.compare_panes.append(frame)
        self.compare_scroll = self._scroller(self.compare_panes)
        self.stack.add_titled(self.compare_scroll, "compare", "Compare")

    @staticmethod
    def _scroller(child: Gtk.Widget | None) -> Gtk.ScrolledWindow:
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        scroller.set_child(child)
        return scroller

    def _build_breakpoints(self) -> None:
        rules = [
            ("min-width: 560px and min-aspect-ratio: 6/5", None, True),
            ("min-width: 900px and min-height: 800px and max-aspect-ratio: 3/2", "roomy", False),
            (
                (
                    "min-width: 900px and min-height: 800px and min-aspect-ratio: 6/5 "
                    "and max-aspect-ratio: 3/2"
                ),
                "roomy",
                True,
            ),
            (
                "min-width: 420px and max-width: 519px and min-height: 300px and max-height: 560px",
                "reduced",
                False,
            ),
            (
                (
                    "min-width: 420px and max-width: 519px and min-height: 300px "
                    "and max-height: 560px and min-aspect-ratio: 6/5"
                ),
                "reduced",
                True,
            ),
            ("max-width: 279px", "mini", False),
            (
                (
                    "min-width: 400px and max-width: 599px and max-height: 280px "
                    "and min-aspect-ratio: 8/5"
                ),
                "mini",
                True,
            ),
        ]
        for condition, density, landscape in rules:
            layout_rule = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(condition))
            layout_rule.add_setter(
                self.activity_panes,
                "orientation",
                Gtk.Orientation.HORIZONTAL if landscape else Gtk.Orientation.VERTICAL,
            )
            classes = ["history-body", density] if density else ["history-body"]
            layout_rule.add_setter(
                self.body,
                "css-classes",
                GObject.Value(GObject.TYPE_STRV, classes),
            )
            self._adaptive.add_breakpoint(layout_rule)
        self._adaptive.connect("notify::current-breakpoint", self._on_layout_changed)

    def _on_page_changed(self, *_args: object) -> None:
        comparing = self.stack.get_visible_child_name() == "compare"
        (self.btn_compare if comparing else self.btn_activities).set_active(True)

    def _on_width_changed(self, *_args: object) -> None:
        columns = (
            2 if self._landscape else (4 if self._adaptive.get_width() >= 480 * self.density else 2)
        )
        self.summary_metrics.set_min_children_per_line(columns)
        self.summary_metrics.set_max_children_per_line(columns)

    def _on_layout_changed(self, *_args: object) -> None:
        scale = next(
            (
                value
                for name, value in (("roomy", 2.0), ("reduced", 0.8), ("mini", 2 / 3))
                if self.body.has_css_class(name)
            ),
            1.0,
        )
        landscape = self.activity_panes.get_orientation() is Gtk.Orientation.HORIZONTAL
        if landscape != self._landscape:
            self._move_activity_list(landscape=landscape)
        self._landscape, self.density = landscape, scale
        for widget in (
            self.body,
            self.sidebar,
            self.activity_panes,
            self.activity_content,
            self.compare_panes,
            self.compare_controls,
        ):
            widget.set_spacing(round(8 * scale))
        for side in ("top", "bottom", "start", "end"):
            getattr(self.body, f"set_margin_{side}")(round(8 * scale))
        self.activity_scroll.set_size_request(round(210 * scale) if landscape else -1, -1)
        self.activity_scroll.set_hexpand(not landscape)
        filters = self.sidebar.get_first_child()
        columns = 2 if not landscape or self._adaptive.get_height() < 360 * scale else 1
        filters.set_min_children_per_line(columns)
        filters.set_max_children_per_line(columns)
        self.compare_panes.set_orientation(
            Gtk.Orientation.HORIZONTAL if landscape else Gtk.Orientation.VERTICAL,
        )
        self.compare_controls.set_size_request(round(210 * scale) if landscape else -1, -1)
        self.compare_controls.set_hexpand(not landscape)
        self.legend.set_visible(landscape)
        self.legend.set_size_request(-1, round(64 * scale))
        self.legend_menu.set_visible(not landscape)
        self.chart.canvas.set_size_request(round(140 * scale), round(160 * scale))
        dpi = self.get_settings().get_property("gtk-xft-dpi")
        self.chart.set_display_scale(scale * (dpi / (96 * 1024) if dpi > 0 else 1))
        self._on_width_changed()

    def _move_activity_list(self, *, landscape: bool) -> None:
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
            target = self.list_scroll if landscape else self.activity_scroll
            offset = (
                self.sidebar.get_height() + self.activity_content.get_spacing()
                if not landscape and position
                else 0
            )
            target.get_vadjustment().set_value(position + offset)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(50, restore)

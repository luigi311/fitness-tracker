from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING

import gi
from loguru import logger
from workout_parser import WorkoutParserError, load_workout

from fitness_tracker.core.environment import Environment, EnvironmentSpec
from fitness_tracker.core.layout import LayoutFamily
from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.ui.layout import layout_frame, layout_owner
from fitness_tracker.workouts import discover_workouts, format_workout_summary, load_test_workout

gi.require_versions({"Gtk": "4.0", "Adw": "1"})

from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402  # ty:ignore[unresolved-import]

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from workout_parser.models import Workout

    from fitness_tracker.core.units import UnitSystem


_ENV_SPECS: dict[Environment, EnvironmentSpec] = {
    Environment.INDOOR: EnvironmentSpec(
        icon="🏠",
        label="Indoor",
        badges=(
            {
                "label": "Incline ctrl",
                "css_class": "badge-amber",
                "sport_type": SportTypesEnum.running,
            },
        ),
    ),
    Environment.OUTDOOR: EnvironmentSpec(
        icon="🌲",
        label="Outdoor",
    ),
    Environment.TRAINER: EnvironmentSpec(
        icon="⚡",
        label="Trainer",
        badges=({"label": "ERG mode", "css_class": "badge-info"},),
    ),
}

_SPORT_ACTIVITY_LABEL = {
    SportTypesEnum.running: "Run",
    SportTypesEnum.biking: "Ride",
}


def _make_badge(text: str, style_class: str) -> Gtk.Label:
    """Create a small pill-shaped badge label."""
    lbl = Gtk.Label(label=text)
    lbl.add_css_class("badge")
    lbl.add_css_class(style_class)
    return lbl


class _EnvCard(Gtk.ToggleButton):
    """A keyboard-accessible environment toggle with an icon, name, and badges."""

    def __init__(
        self,
        environment: Environment,
        on_clicked: Callable[[Environment], None],
    ) -> None:
        """Build a selectable environment card with its icon and capability badges."""
        super().__init__()
        self.environment = environment

        self.add_css_class("env-card")
        self.set_hexpand(True)
        self.set_cursor(Gdk.Cursor.new_from_name("pointer"))

        # Inner box holds the actual content with padding
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for m in ("top", "bottom"):
            getattr(inner, f"set_margin_{m}")(6)

        for m in ("start", "end"):
            getattr(inner, f"set_margin_{m}")(4)

        inner.set_halign(Gtk.Align.CENTER)
        self.set_child(inner)

        spec = _ENV_SPECS[environment]
        self.update_property([Gtk.AccessibleProperty.LABEL], [spec.label])
        self._icon_lbl = Gtk.Label(label=spec.icon)
        self._icon_lbl.add_css_class("env-icon")
        inner.append(self._icon_lbl)

        self._name_lbl = Gtk.Label(label=spec.label)
        self._name_lbl.add_css_class("env-name")
        inner.append(self._name_lbl)

        self._badge_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._badge_box.set_halign(Gtk.Align.CENTER)
        inner.append(self._badge_box)

        self.connect("clicked", lambda *_: on_clicked(environment))

    def update_badges(self, sport_type: SportTypesEnum) -> None:
        for child in list(self._badge_box):
            self._badge_box.remove(child)
        for badge in _ENV_SPECS[self.environment].badges:
            if badge.sport_type is not None and badge.sport_type != sport_type:
                continue
            self._badge_box.append(_make_badge(badge.label, badge.css_class))

    def set_selected(self, *, selected: bool) -> None:
        """Update the card selection state and its visual treatment."""
        self.set_active(selected)
        # Always remove both classes first, unconditionally.
        self.remove_css_class("env-card-selected")
        self._name_lbl.remove_css_class("env-name-selected")

        if selected:
            self.add_css_class("env-card-selected")
            self._name_lbl.add_css_class("env-name-selected")


# ---------------------------------------------------------------------------
# CSS injected once at import time
# ---------------------------------------------------------------------------
_CSS = b"""
/* ---- Environment cards ---- */
.env-card {
    border-radius: 12px;
    background: transparent;
    border: 2px solid alpha(@borders, 0.6);
    padding: 2px 4px;
}
.env-card-selected {
    border: 2px solid @accent_color;
    background: alpha(@accent_color, 0.12);
}
.env-name-selected {
    color: @accent_color;
    font-weight: 600;
}
.env-icon {
    font-size: 22px;
}
.env-name {
    font-size: 13px;
    font-weight: 500;
}

/* ---- Feature badges ---- */
.badge {
    font-size: 10px;
    font-weight: 600;
    border-radius: 8px;
    padding: 2px 8px;
}
.badge-success {
    background: alpha(#4caf50, 0.20);
    color: #388e3c;
}
.badge-info {
    background: alpha(@accent_color, 0.18);
    color: @accent_color;
}
.badge-amber {
    background: alpha(#ff9800, 0.18);
    color: #e65100;
}
.badge-neutral {
    background: alpha(@borders, 0.35);
    color: alpha(@window_fg_color, 0.55);
}

/* ---- Start button ---- */
.start-free-btn {
    font-size: 14px;
    font-weight: 600;
    border-radius: 8px;
    padding: 10px 0;
}

/* ---- Section label ---- */
.section-label {
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.06em;
    color: alpha(@window_fg_color, 0.45);
}

/* ---- Workout list ---- */
.workout-frame row {
    padding: 0;
}
"""


@cache
def _ensure_css() -> None:
    provider = Gtk.CssProvider()
    provider.load_from_data(_CSS)
    Gtk.StyleContext.add_provider_for_display(
        provider.get_display() if hasattr(provider, "get_display") else Gdk.Display.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )


# ---------------------------------------------------------------------------
# Public view
# ---------------------------------------------------------------------------
class ModeSelectView(Gtk.Box):
    """
    Landing tracker selector with:
      - Run / Bike sport switcher (segmented buttons)
      - Environment selector: Indoor / Outdoor / Trainer cards
        Each card shows contextual feature badges.
      - "Start Free <env> <activity>" button
      - Scrollable workout list filtered by sport.

    Callbacks:
      on_start_free(sport_type, environment)
      on_start_workout(workout, sport_type, environment)
    """

    def __init__(
        self,
        workouts_running_dir: Path,
        workouts_cycling_dir: Path,
        on_start_free: Callable[..., None],
        on_start_workout: Callable[..., None],
        *,
        unit_system: UnitSystem,
        test_mode: bool = False,
    ) -> None:
        """Build adaptive sport, environment, and workout selection controls."""
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        _ensure_css()
        self.add_css_class("mode-select")
        for m in ("top", "bottom"):
            getattr(self, f"set_margin_{m}")(6)

        for m in ("start", "end"):
            getattr(self, f"set_margin_{m}")(4)

        self._workouts_running_dir = workouts_running_dir
        self._workouts_cycling_dir = workouts_cycling_dir
        self._unit_system = unit_system
        self._test_mode = test_mode

        self._on_start_free = on_start_free
        self._on_start_workout = on_start_workout

        # Current mode
        self.sport_type: SportTypesEnum = SportTypesEnum.running
        self._selected_env = Environment.INDOOR

        self._setup = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._setup_pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._setup_pane.set_hexpand(True)
        self._library = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._library.set_hexpand(True)
        self._library.set_vexpand(True)
        setup_scroll = Gtk.ScrolledWindow()
        setup_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        setup_scroll.set_propagate_natural_height(True)
        setup_scroll.set_child(self._setup)
        setup_scroll.set_hexpand(True)
        self._setup_pane.append(setup_scroll)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.append(self._setup_pane)
        body.append(self._library)
        adaptive = Adw.BreakpointBin()
        adaptive.set_size_request(200, 160)
        adaptive.set_vexpand(True)
        adaptive.set_child(body)
        self.append(adaptive)
        self._setup.append(self._build_sport_switch())
        self._build_environment_selector()
        self._build_start_button()
        self._build_workout_panel()
        self._build_compact_breakpoints(adaptive, body, setup_scroll)

        # Defer initial population until after the widget is realized so the
        # CSS provider has been fully cascaded before set_selected() runs.
        self.connect("realize", lambda *_: self.refresh())

    def _build_compact_breakpoints(
        self,
        adaptive: Adw.BreakpointBin,
        body: Gtk.Box,
        setup_scroll: Gtk.ScrolledWindow,
    ) -> None:
        """Use a shared reference size and choose setup panes only from aspect."""
        self._body, self._setup_scroll = body, setup_scroll
        self._layout_owner = None
        self._layout_handler_id = None
        adaptive.add_breakpoint(
            Adw.Breakpoint.new(Adw.BreakpointCondition.parse("min-width: 0px")),
        )
        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)

    def _on_map(self, *_args: object) -> None:
        """Observe the window's aspect policy while the selector is visible."""
        self._layout_owner = layout_owner(self)
        if self._layout_owner is not None:
            self._layout_handler_id = self._layout_owner.connect(
                "layout-changed",
                self._on_layout_changed,
            )
        self._on_layout_changed()

    def _on_unmap(self, *_args: object) -> None:
        """Disconnect the shared policy before this selector leaves the screen."""
        if self._layout_handler_id is not None:
            self._layout_owner.disconnect(self._layout_handler_id)
            self._layout_handler_id = None
        self._layout_owner = None

    def _on_layout_changed(self, *_args: object) -> None:
        """Place selection beside the workout list on landscape-shaped windows."""
        frame = layout_frame(self)
        landscape = frame.family is LayoutFamily.LANDSCAPE or (
            frame.family is LayoutFamily.SQUARE and frame.width >= frame.height
        )
        self._body.set_orientation(
            Gtk.Orientation.HORIZONTAL if landscape else Gtk.Orientation.VERTICAL,
        )
        self._body.set_homogeneous(landscape)
        self._setup_scroll.set_vexpand(landscape)

    def _build_sport_switch(self) -> Gtk.Box:
        """Build the Run/Bike segmented switcher."""
        switch_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        switch_row.add_css_class("linked")
        switch_row.set_halign(Gtk.Align.FILL)

        self._btn_run = Gtk.ToggleButton.new_with_label("Run")
        self._btn_cycle = Gtk.ToggleButton.new_with_label("Bike")
        self._btn_cycle.set_group(self._btn_run)
        for b in (self._btn_run, self._btn_cycle):
            b.add_css_class("flat")
            b.set_hexpand(True)
            b.set_size_request(-1, 48)

        self._btn_run.set_active(True)
        self._btn_run.connect("toggled", self._on_mode_toggled, SportTypesEnum.running)
        self._btn_cycle.connect("toggled", self._on_mode_toggled, SportTypesEnum.biking)

        switch_row.append(self._btn_run)
        switch_row.append(self._btn_cycle)
        return switch_row

    def _build_environment_selector(self) -> None:
        """Build the environment label and selectable cards."""
        env_label = Gtk.Label(label="Environment")
        env_label.add_css_class("section-label")
        env_label.set_xalign(0)
        self._setup.append(env_label)

        card_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        card_row.set_halign(Gtk.Align.FILL)

        self._env_cards: dict[Environment, _EnvCard] = {}
        for environment in Environment:
            card = _EnvCard(environment, on_clicked=self._on_env_card_clicked)
            self._env_cards[environment] = card
            card_row.append(card)

        self._setup.append(card_row)

    def _build_start_button(self) -> None:
        """Build the free-session start button."""
        self._btn_start_free = Gtk.Button()
        self._btn_start_free.add_css_class("suggested-action")
        self._btn_start_free.add_css_class("start-free-btn")
        self._btn_start_free.connect("clicked", self._on_start_free_clicked)
        self._setup_pane.append(self._btn_start_free)

    def _build_workout_panel(self) -> None:
        """Build the workout list and its scrolling frame."""
        workouts_label = Gtk.Label(label="Workouts")
        workouts_label.add_css_class("section-label")
        workouts_label.set_xalign(0)
        self._library.append(workouts_label)

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)

        sc = Gtk.ScrolledWindow()
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sc.set_vexpand(True)
        sc.set_child(self._list)

        frame = Gtk.Frame()
        frame.add_css_class("workout-frame")
        frame.set_child(sc)
        self._library.append(frame)

    def refresh(self) -> None:
        """Re-scan workouts dir and repopulate without duplicating UI."""
        self._sync_env_selection()
        self._update_start_button_label()
        self._rebuild_workout_list()

    def set_unit_system(self, unit_system: UnitSystem) -> None:
        """Refresh workout summaries after the display units change."""
        if unit_system == self._unit_system:
            return
        self._unit_system = unit_system
        self._rebuild_workout_list()

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------
    def _update_start_button_label(self) -> None:
        activity = _SPORT_ACTIVITY_LABEL.get(self.sport_type, "Session")
        env_name = _ENV_SPECS[self._selected_env].label
        self._btn_start_free.set_label(f"Start Free {env_name} {activity}")

    def _sync_env_selection(self) -> None:
        for environment, card in self._env_cards.items():
            card.set_selected(selected=environment == self._selected_env)
            card.update_badges(self.sport_type)

    def _rebuild_workout_list(self) -> None:
        # Determine entries
        self._entries: list[tuple[Workout, str]] = []
        if self.sport_type == SportTypesEnum.running:
            workout_files = (
                (workout_path, "run")
                for workout_path in discover_workouts(self._workouts_running_dir)
            )
        elif self.sport_type == SportTypesEnum.biking:
            workout_files = (
                (workout_path, "cycle")
                for workout_path in discover_workouts(self._workouts_cycling_dir)
            )
        else:
            workout_files = iter(())

        for workout_path, sport in workout_files:
            try:
                workout = load_workout(workout_path)
            except WorkoutParserError as error:
                logger.warning(f"Skipping invalid workout {workout_path}: {error}")
                continue
            self._entries.append((workout, sport))

        self._add_test_workout()

        # clear all rows
        for row in list(self._list):
            self._list.remove(row)

        if not self._entries:
            empty = Adw.ActionRow()
            empty.set_title("No workouts found")
            empty.set_subtitle("Add workouts to your workouts directory.")
            empty.set_activatable(False)
            self._list.append(empty)
            return

        # repopulate
        for workout, _ in self._entries:
            row = Adw.ActionRow()
            row.set_use_markup(False)
            row.set_title(workout.name)
            row.set_title_lines(1)
            row.set_subtitle_lines(1)

            # Workout information
            subtitle_parts: list[str] = []
            if workout.workout_date:
                subtitle_parts.append(workout.workout_date.isoformat())
            summary = format_workout_summary(workout, self._unit_system)
            if summary:
                subtitle_parts.append(summary)
            row.set_subtitle(" · ".join(subtitle_parts))

            env_name = _ENV_SPECS[self._selected_env].label
            btn = Gtk.Button.new_with_label(f"Start {env_name}")
            btn.add_css_class("pill")
            btn.set_valign(Gtk.Align.CENTER)
            btn.connect(
                "clicked",
                self._on_row_start_clicked,
                workout,
                self.sport_type,
                self._selected_env,
            )

            row.add_suffix(btn)
            row.set_activatable(False)
            self._list.append(row)

    def _add_test_workout(self) -> None:
        """Include the demo for either sport while preserving same-name user workouts."""
        if not self._test_mode or self.sport_type not in {
            SportTypesEnum.running,
            SportTypesEnum.biking,
        }:
            return
        workout = load_test_workout(self.sport_type)
        if not any(existing.name == workout.name for existing, _ in self._entries):
            sport = "run" if self.sport_type == SportTypesEnum.running else "cycle"
            self._entries.append((workout, sport))

    # -----------------------------------------------------------------------
    # Signal handlers
    # -----------------------------------------------------------------------
    def _on_env_card_clicked(self, environment: Environment) -> None:
        self._selected_env = environment
        self._sync_env_selection()
        self._update_start_button_label()
        self._rebuild_workout_list()

    def _on_start_free_clicked(self, _btn: Gtk.Button) -> None:
        self._on_start_free(
            sport_type=self.sport_type,
            environment=self._selected_env,
        )

    def _on_row_start_clicked(
        self,
        _btn: Gtk.Button,
        workout: Workout,
        sport_type: SportTypesEnum,
        environment: Environment,
    ) -> None:
        self._on_start_workout(
            workout,
            sport_type=sport_type,
            environment=environment,
        )

    def _on_mode_toggled(self, btn: Gtk.ToggleButton, sport_type: SportTypesEnum) -> None:
        # We only react to the button that just became active
        if not btn.get_active():
            return

        # Mutual exclusivity
        if sport_type == SportTypesEnum.running:
            self._btn_cycle.set_active(False)
        elif sport_type == SportTypesEnum.biking:
            self._btn_run.set_active(False)
        else:
            self._btn_run.set_active(False)
            self._btn_cycle.set_active(False)

        self.sport_type = sport_type
        # refresh after toggle settles
        GLib.idle_add(self.refresh)

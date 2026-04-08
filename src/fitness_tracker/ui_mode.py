from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

import gi
from workout_parser.main import load_workout

from fitness_tracker.database import SportTypesEnum
from fitness_tracker.workouts import discover_workouts

gi.require_versions({"Gtk": "4.0", "Adw": "1"})

from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402  # ty:ignore[unresolved-import]

if TYPE_CHECKING:
    from pathlib import Path

    from workout_parser.models import Workout


class IndoorOutdoorEnum(Enum):
    indoor = 1
    outdoor = 2


# ---------------------------------------------------------------------------
# Feature badge definitions for each sport x environment combination.
# Each entry is (label, css_class) where css_class maps to a GTK style class
# applied to the badge label.
# ---------------------------------------------------------------------------
_ENV_BADGES: dict[SportTypesEnum, dict[str, list[tuple[str, str]]]] = {
    SportTypesEnum.running: {
        "indoor": [("Incline ctrl", "badge-amber")],
        "outdoor": [],  # [("GPS", "badge-success")] GPS is not implemented so dont display it yet
        "trainer": [("ERG mode", "badge-info")],
    },
    SportTypesEnum.biking: {
        "indoor": [],  # [("No GPS", "badge-neutral")],
        "outdoor": [],  # [("GPS", "badge-success")] GPS is not implemented so dont display it yet
        "trainer": [("ERG mode", "badge-info")],
    },
}

_ENV_ICONS = {
    "indoor": "🏠",
    "outdoor": "🌲",
    "trainer": "⚡",
}

_ENV_LABELS = {
    "indoor": "Indoor",
    "outdoor": "Outdoor",
    "trainer": "Trainer",
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


class _EnvCard(Gtk.Box):
    """
    A selectable environment card showing icon, name, and feature badges.

    The outer box carries the border/background CSS and click gesture.
    An inner box provides the content padding so margins never interfere
    with border rendering — this is what was causing the sticky highlight.
    """

    def __init__(self, env_key: str, on_clicked) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.env_key = env_key

        self.add_css_class("env-card")
        self.set_hexpand(True)
        self.set_cursor(Gdk.Cursor.new_from_name("pointer"))

        # Inner box holds the actual content with padding
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        inner.set_margin_top(16)
        inner.set_margin_bottom(14)
        inner.set_margin_start(10)
        inner.set_margin_end(10)
        inner.set_halign(Gtk.Align.CENTER)
        self.append(inner)

        self._icon_lbl = Gtk.Label(label=_ENV_ICONS[env_key])
        self._icon_lbl.add_css_class("env-icon")
        inner.append(self._icon_lbl)

        self._name_lbl = Gtk.Label(label=_ENV_LABELS[env_key])
        self._name_lbl.add_css_class("env-name")
        inner.append(self._name_lbl)

        self._badge_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._badge_box.set_halign(Gtk.Align.CENTER)
        inner.append(self._badge_box)

        gesture = Gtk.GestureClick.new()
        gesture.connect("released", lambda *_: on_clicked(env_key))
        self.add_controller(gesture)

    def update_badges(self, sport_type: SportTypesEnum) -> None:
        for child in list(self._badge_box):
            self._badge_box.remove(child)
        entries = _ENV_BADGES.get(sport_type, {}).get(self.env_key, [])
        for text, cls in entries:
            self._badge_box.append(_make_badge(text, cls))

    def set_selected(self, selected: bool) -> None:
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
.workout-frame > list > row {
    padding: 4px 0;
}
"""

_css_loaded = False


def _ensure_css() -> None:
    global _css_loaded
    if _css_loaded:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(_CSS)
    Gtk.StyleContext.add_provider_for_display(
        provider.get_display() if hasattr(provider, "get_display") else Gdk.Display.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    _css_loaded = True


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
      on_start_free(sport_type, in_outdoor, trainer)
      on_start_workout(workout, sport_type, in_outdoor, trainer)
    """

    def __init__(
        self,
        workouts_running_dir: Path,
        workouts_cycling_dir: Path,
        on_start_free,
        on_start_workout,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        _ensure_css()
        for m in ("top", "bottom", "start", "end"):
            getattr(self, f"set_margin_{m}")(12)

        self._workouts_running_dir = workouts_running_dir
        self._workouts_cycling_dir = workouts_cycling_dir

        self._on_start_free = on_start_free
        self._on_start_workout = on_start_workout

        # Current mode
        self.sport_type: SportTypesEnum = SportTypesEnum.running
        self._selected_env: str = "indoor"  # "indoor" | "outdoor" | "trainer"

        # --- Switcher (segmented buttons) ---
        switch_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        switch_row.add_css_class("linked")
        switch_row.set_halign(Gtk.Align.FILL)

        self._btn_run = Gtk.ToggleButton.new_with_label("Run")
        self._btn_cycle = Gtk.ToggleButton.new_with_label("Bike")
        for b in (self._btn_run, self._btn_cycle):
            b.add_css_class("flat")
            b.set_hexpand(True)

        self._btn_run.set_active(True)
        self._btn_run.connect("toggled", self._on_mode_toggled, SportTypesEnum.running)
        self._btn_cycle.connect("toggled", self._on_mode_toggled, SportTypesEnum.biking)

        switch_row.append(self._btn_run)
        switch_row.append(self._btn_cycle)
        self.append(switch_row)

        # ---- Environment label + card grid ---------------------------------
        env_label = Gtk.Label(label="Environment")
        env_label.add_css_class("section-label")
        env_label.set_xalign(0)
        self.append(env_label)

        card_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        card_row.set_halign(Gtk.Align.FILL)

        self._env_cards: dict[str, _EnvCard] = {}
        for key in ("indoor", "outdoor", "trainer"):
            card = _EnvCard(key, on_clicked=self._on_env_card_clicked)
            self._env_cards[key] = card
            card_row.append(card)

        self.append(card_row)

        # ---- Start Free button ---------------------------------------------
        self._btn_start_free = Gtk.Button()
        self._btn_start_free.add_css_class("suggested-action")
        self._btn_start_free.add_css_class("start-free-btn")
        self._btn_start_free.connect("clicked", self._on_start_free_clicked)
        self.append(self._btn_start_free)

        # Workout list UI
        workouts_label = Gtk.Label(label="Workouts")
        workouts_label.add_css_class("section-label")
        workouts_label.set_xalign(0)
        self.append(workouts_label)

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)

        sc = Gtk.ScrolledWindow()
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sc.set_vexpand(True)
        sc.set_child(self._list)

        frame = Gtk.Frame()
        frame.add_css_class("workout-frame")
        frame.set_child(sc)
        self.append(frame)

        # Defer initial population until after the widget is realized so the
        # CSS provider has been fully cascaded before set_selected() runs.
        self.connect("realize", lambda *_: self.refresh())

    def refresh(self) -> None:
        """Re-scan workouts dir and repopulate without duplicating UI."""
        self._sync_env_selection()
        self._update_start_button_label()
        self._rebuild_workout_list()

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------
    def _env_to_params(self) -> tuple[IndoorOutdoorEnum, bool]:
        """Convert the selected env key to (in_outdoor, trainer) params."""
        if self._selected_env == "outdoor":
            return IndoorOutdoorEnum.outdoor, False
        if self._selected_env == "trainer":
            return IndoorOutdoorEnum.indoor, True
        return IndoorOutdoorEnum.indoor, False

    def _update_start_button_label(self) -> None:
        activity = _SPORT_ACTIVITY_LABEL.get(self.sport_type, "Session")
        env_name = _ENV_LABELS[self._selected_env]
        self._btn_start_free.set_label(f"Start Free {env_name} {activity}")

    def _sync_env_selection(self) -> None:
        for key, card in self._env_cards.items():
            card.set_selected(key == self._selected_env)
            card.update_badges(self.sport_type)

    def _rebuild_workout_list(self) -> None:
        # Determine entries
        self._entries: list[tuple[Workout, str]] = []
        if self.sport_type == SportTypesEnum.running:
            for workout in discover_workouts(self._workouts_running_dir):
                self._entries.append((load_workout(workout), "run"))
        elif self.sport_type == SportTypesEnum.biking:
            for workout in discover_workouts(self._workouts_cycling_dir):
                self._entries.append((load_workout(workout), "cycle"))

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
            row.set_title(workout.name)

            # Workout information
            subtitle = ""
            if workout.workout_date:
                subtitle += workout.workout_date.isoformat()
            if workout.total_seconds:
                if subtitle:
                    subtitle += " · "
                mins = workout.total_seconds // 60
                subtitle += f"{mins} min"
            row.set_subtitle(subtitle)

            in_outdoor, trainer = self._env_to_params()
            env_name = _ENV_LABELS[self._selected_env]
            btn = Gtk.Button.new_with_label(f"Start {env_name}")
            btn.add_css_class("pill")
            btn.set_valign(Gtk.Align.CENTER)
            btn.connect(
                "clicked",
                self._on_row_start_clicked,
                workout,
                self.sport_type,
                in_outdoor,
                trainer,
            )

            row.add_suffix(btn)
            row.set_activatable(False)
            self._list.append(row)

    # -----------------------------------------------------------------------
    # Signal handlers
    # -----------------------------------------------------------------------
    def _on_env_card_clicked(self, env_key: str) -> None:
        self._selected_env = env_key
        self._sync_env_selection()
        self._update_start_button_label()
        self._rebuild_workout_list()

    def _on_start_free_clicked(self, _btn: Gtk.Button) -> None:
        in_outdoor, trainer = self._env_to_params()
        self._on_start_free(
            sport_type=self.sport_type,
            in_outdoor=in_outdoor,
            trainer=trainer,
        )

    def _on_row_start_clicked(
        self,
        _btn: Gtk.Button,
        workout: Workout,
        sport_type: SportTypesEnum,
        in_outdoor: IndoorOutdoorEnum,
        trainer: bool = False,
    ) -> None:
        self._on_start_workout(
            workout,
            sport_type=sport_type,
            in_outdoor=in_outdoor,
            trainer=trainer,
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

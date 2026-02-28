from __future__ import annotations

from typing import TYPE_CHECKING

import gi

from fitness_tracker.database import SportTypesEnum
from fitness_tracker.workouts import discover_workouts

gi.require_versions({"Gtk": "4.0", "Adw": "1"})

from gi.repository import Adw, GLib, Gtk

if TYPE_CHECKING:
    from pathlib import Path


class ModeSelectView(Gtk.Box):
    """
    Landing tracker selector:
      - Run / Cycle switcher
      - Start Free X (label depends on mode)
      - Workouts list filtered by mode; each row has a Start button
    Calls the provided callbacks when a selection is made.
    """

    def __init__(
        self,
        workouts_running_dir: Path,
        workouts_cycling_dir: Path,
        on_start_free,
        on_start_workout,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for m in ("top", "bottom", "start", "end"):
            getattr(self, f"set_margin_{m}")(12)

        self._workouts_running_dir = workouts_running_dir
        self._workouts_cycling_dir = workouts_cycling_dir

        self._on_start_free = on_start_free
        self._on_start_workout = on_start_workout

        # Current mode
        self.sport_type: SportTypesEnum = SportTypesEnum.running

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

        # --- Start Free button ---
        self._btn_free = Gtk.Button()
        self._btn_free.add_css_class("suggested-action")
        self._btn_free.set_halign(Gtk.Align.FILL)
        self._btn_free.set_hexpand(True)
        self._btn_free.connect("clicked", lambda *_: self._on_start_free(self.sport_type))
        self.append(self._btn_free)

        # Workout list UI
        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)

        sc = Gtk.ScrolledWindow()
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sc.set_vexpand(True)
        sc.set_child(self._list)

        frame = Gtk.Frame(label="Workouts")
        frame.set_child(sc)
        self.append(frame)

        # initial population
        self.refresh()

    def refresh(self) -> None:
        """Re-scan the workouts dir and repopulate the list without duplicating UI."""
        # Update "Start Free" label
        if self.sport_type == SportTypesEnum.running:
            self._btn_free.set_label("Start Free Run")
        elif self.sport_type == SportTypesEnum.biking:
            self._btn_free.set_label("Start Free Ride")
        else:
            self._btn_free.set_label("Start Free Session")

        # Build list of (path, kind) depending on mode
        entries: list[tuple[Path, str]] = []
        if self.sport_type == SportTypesEnum.running:
            for p in discover_workouts(self._workouts_running_dir):
                entries.append((p, "run"))
        if self.sport_type == SportTypesEnum.biking:
            for p in discover_workouts(self._workouts_cycling_dir):
                entries.append((p, "cycle"))

        # Sort by display name (stable)
        entries.sort(key=lambda t: t[0].stem.lower())
        self._entries = entries

        # clear all rows
        for row in list(self._list):  # Gtk.ListBox is iterable over rows
            self._list.remove(row)

        # repopulate
        for p, kind in self._entries:
            row = Adw.ActionRow()
            # Title: workout name, Subtitle: file type + kind
            row.set_title(p.stem)
            row.set_subtitle(f"{kind.upper()} • {p.suffix.lower().lstrip('.').upper()}")

            start_btn = Gtk.Button.new_with_label("Free")
            start_btn.add_css_class("pill")
            start_btn.connect("clicked", self._on_row_start_clicked, p, self.sport_type, False)
            start_btn_trainer = Gtk.Button.new_with_label("Trainer")
            start_btn_trainer.add_css_class("pill")
            start_btn_trainer.connect(
                "clicked", self._on_row_start_clicked, p, self.sport_type, True
            )
            row.add_suffix(start_btn)
            row.add_suffix(start_btn_trainer)
            row.set_activatable(False)

            self._list.append(row)

        # Optional empty state: show a single row if none
        if not self._entries:
            empty = Adw.ActionRow()
            empty.set_title("No workouts found")
            empty.set_subtitle("Add workouts to your workouts directory.")
            empty.set_activatable(False)
            self._list.append(empty)

    def _on_row_start_clicked(
        self,
        _btn: Gtk.Button,
        path: Path,
        sport_type: SportTypesEnum,
        trainer: bool = False,
    ) -> None:
        self._on_start_workout(path, sport_type=sport_type, trainer=trainer)

    def _on_mode_toggled(self, btn: Gtk.ToggleButton, sport_type: SportTypesEnum) -> None:
        # We only react to the button that just became active
        if not btn.get_active():
            return

        # Ensure mutual exclusivity (Gtk.ToggleButton doesn't auto-group)
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

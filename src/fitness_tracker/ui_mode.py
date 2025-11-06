from __future__ import annotations

from pathlib import Path

import gi

from fitness_tracker.workouts import discover_workouts

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Gtk, Adw, GLib


class ModeSelectView(Gtk.Box):
    """
    Simple selector page:
      - Start Free Run
      - List available workouts and Start Selected
    Calls the provided callbacks when a selection is made.
    """

    def __init__(
        self,
        workouts_running_dir: Path,
        on_start_free_run,
        on_start_workout,  # (path: Path) -> None
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for m in ("top", "bottom", "start", "end"):
            getattr(self, f"set_margin_{m}")(12)

        self._workouts_running_dir = workouts_running_dir
        self._on_start_free_run = on_start_free_run
        self._on_start_workout = on_start_workout

        title = Gtk.Label(label="How do you want to train?")
        title.add_css_class("title-1")
        title.set_halign(Gtk.Align.CENTER)
        self.append(title)

        # Workout list UI
        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.SINGLE)

        sc = Gtk.ScrolledWindow()
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sc.set_vexpand(True)
        sc.set_child(self._list)

        frame = Gtk.Frame(label="Workouts")
        frame.set_child(sc)
        self.append(frame)

        btn_free = Gtk.Button.new_with_label("Start Free Run")
        btn_free.add_css_class("suggested-action")
        btn_free.set_halign(Gtk.Align.CENTER)
        btn_free.connect("clicked", lambda *_: self._on_start_free_run())

        self._btn_start_w = Gtk.Button.new_with_label("Start Selected Workout")
        self._btn_start_w.set_sensitive(False)  # updated in refresh()
        self._btn_start_w.set_halign(Gtk.Align.CENTER)
        self._btn_start_w.connect("clicked", self._on_start_selected_clicked)

        # --- FlowBox containing just the two pairs ---
        controls = Gtk.FlowBox()
        controls.set_selection_mode(Gtk.SelectionMode.NONE)
        controls.set_homogeneous(True)     # nav_pair and act_pair get equal cell widths
        controls.set_column_spacing(8)
        controls.set_row_spacing(8)
        controls.set_min_children_per_line(1)   # stacks on very narrow screens
        controls.set_max_children_per_line(2)   # side-by-side when there’s room

        controls.insert(btn_free, -1)
        controls.insert(self._btn_start_w, -1)

        self.append(controls)

        # initial population
        self.refresh()

    def refresh(self) -> None:
            """Re-scan the workouts dir and repopulate the list without duplicating UI."""
            # discover and sort
            self._paths = discover_workouts(self._workouts_running_dir)

            # clear all rows
            for row in list(self._list):   # Gtk.ListBox is iterable over rows
                self._list.remove(row)

            # repopulate
            for p in self._paths:
                row = Adw.ActionRow()
                row.set_title(p.stem)
                row.set_subtitle(p.suffix.lower().lstrip(".").upper())
                self._list.append(row)

            self._btn_start_w.set_sensitive(bool(self._paths))
            self._btn_start_w.add_css_class("suggested-action")

            # select first row (async so rows are realized)
            if self._paths:
                GLib.idle_add(lambda: self._list.select_row(self._list.get_row_at_index(0)))

    def _on_start_selected_clicked(self, *_):
        row = self._list.get_selected_row()
        if not row:
            return
        idx = row.get_index()
        self._on_start_workout(self._paths[idx])

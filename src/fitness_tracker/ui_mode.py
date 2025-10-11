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
        workouts_dir: Path,
        on_start_free_run,
        on_start_workout,  # (path: Path) -> None
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(self, f"set_margin_{m}")(12)

        self._workouts_dir = workouts_dir
        self._on_start_free_run = on_start_free_run
        self._on_start_workout = on_start_workout

        title = Gtk.Label(label="How do you want to train?")
        title.add_css_class("title-1")
        title.set_halign(Gtk.Align.CENTER)
        self.append(title)

        btn_free = Gtk.Button.new_with_label("Start Free Run")
        btn_free.add_css_class("suggested-action")
        btn_free.set_halign(Gtk.Align.CENTER)
        btn_free.connect("clicked", lambda *_: self._on_start_free_run())
        self.append(btn_free)

        # Workout list
        self._paths = discover_workouts(self._workouts_dir)
        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.SINGLE)

        for p in self._paths:
            row = Adw.ActionRow()
            row.set_title(p.stem)
            row.set_subtitle(p.suffix.lower().lstrip(".").upper())
            self._list.append(row)

        if self._paths:
            GLib.idle_add(lambda: self._list.select_row(self._list.get_row_at_index(0)))

        sc = Gtk.ScrolledWindow()
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sc.set_vexpand(True)
        sc.set_child(self._list)

        frame = Gtk.Frame(label="Workouts")
        frame.set_child(sc)
        self.append(frame)

        self._btn_start_w = Gtk.Button.new_with_label("Start Selected Workout")
        self._btn_start_w.set_sensitive(bool(self._paths))
        self._btn_start_w.set_halign(Gtk.Align.CENTER)
        self._btn_start_w.connect("clicked", self._on_start_selected_clicked)
        self.append(self._btn_start_w)

    def _on_start_selected_clicked(self, *_):
        row = self._list.get_selected_row()
        if not row:
            return
        idx = row.get_index()
        self._on_start_workout(self._paths[idx])

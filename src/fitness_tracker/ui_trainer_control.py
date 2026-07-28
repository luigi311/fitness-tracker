from __future__ import annotations

import gi

from fitness_tracker.database import SportTypesEnum

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402  # ty:ignore[unresolved-import]


class TrainerTargetControl(Gtk.Frame):
    """Sport-specific trainer target control."""

    BIKE_MODES = {
        "Power": {"minimum": 0, "maximum": 2000, "step": 5, "unit": "W"},
        "Resistance": {"minimum": 0, "maximum": 100, "step": 1, "unit": "%"},
    }
    RUN_MODES = {
        "Speed": {"minimum": 0.0, "maximum": 15.0, "step": 0.1, "unit": "mph"},
    }

    def __init__(self, on_change, sport_type: SportTypesEnum) -> None:
        super().__init__()
        self._on_change = on_change
        self.MODES = self.BIKE_MODES if sport_type == SportTypesEnum.biking else self.RUN_MODES
        self._mode = "Power" if sport_type == SportTypesEnum.biking else "Speed"
        self._values = {"Power": 100, "Resistance": 2, "Speed": 3.0}

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for margin in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{margin}")(8)

        if sport_type == SportTypesEnum.biking:
            mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            mode_row.set_homogeneous(True)
            mode_row.set_hexpand(True)

            self._power_mode_btn = Gtk.Button(label="Power")
            self._power_mode_btn.set_size_request(-1, 56)
            self._power_mode_btn.add_css_class("suggested-action")

            self._resistance_mode_btn = Gtk.Button(label="Resistance")
            self._resistance_mode_btn.set_size_request(-1, 56)

            self._power_mode_btn.connect("clicked", self._on_mode_clicked, "Power")
            self._resistance_mode_btn.connect("clicked", self._on_mode_clicked, "Resistance")
            mode_row.append(self._power_mode_btn)
            mode_row.append(self._resistance_mode_btn)
            outer.append(mode_row)
        else:
            title = Gtk.Label(label="Speed")
            title.add_css_class("caption")
            outer.append(title)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._btn_down = Gtk.Button(label="-")
        self._btn_down.add_css_class("destructive-action")
        self._btn_down.set_hexpand(True)
        self._btn_down.set_size_request(-1, 72)
        self._btn_down.get_child().add_css_class("title-1")
        self._btn_down.connect("clicked", lambda *_: self._change(-1))

        self._lbl_value = Gtk.Label()
        self._lbl_value.add_css_class("title-1")
        self._lbl_value.add_css_class("numeric")
        self._lbl_value.set_hexpand(True)
        self._lbl_value.set_xalign(0.5)

        self._btn_up = Gtk.Button(label="+")
        self._btn_up.add_css_class("suggested-action")
        self._btn_up.set_hexpand(True)
        self._btn_up.set_size_request(-1, 72)
        self._btn_up.get_child().add_css_class("title-1")
        self._btn_up.connect("clicked", lambda *_: self._change(1))

        row.append(self._btn_down)
        row.append(self._lbl_value)
        row.append(self._btn_up)
        outer.append(row)
        self.set_child(outer)
        self._refresh()

    def _change(self, direction: int) -> None:
        config = self.MODES[self._mode]
        value = self._values[self._mode] + direction * config["step"]
        value = max(config["minimum"], min(config["maximum"], value))
        self._values[self._mode] = round(value, 1) if self._mode == "Speed" else value
        self._refresh()
        self._on_change(self._mode, self._values[self._mode])

    def _on_mode_clicked(self, _button, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        self._power_mode_btn.set_css_classes(["suggested-action"] if mode == "Power" else [])
        self._resistance_mode_btn.set_css_classes(
            ["suggested-action"] if mode == "Resistance" else [],
        )
        self._refresh()
        self._on_change(self._mode, self._values[self._mode])

    def _refresh(self) -> None:
        config = self.MODES[self._mode]
        value = self._values[self._mode]
        value_text = f"{value:.1f}" if self._mode == "Speed" else str(value)
        self._lbl_value.set_text(f"{value_text} {config['unit']}")
        self._btn_down.set_sensitive(value > config["minimum"])
        self._btn_up.set_sensitive(value < config["maximum"])

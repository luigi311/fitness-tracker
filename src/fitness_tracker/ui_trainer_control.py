from __future__ import annotations

from typing import ClassVar

import gi

from fitness_tracker.database import SportTypesEnum

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402  # ty:ignore[unresolved-import]


class TrainerTargetControl(Gtk.Frame):
    """Touch-friendly control for selecting and adjusting trainer targets."""

    MODE_CONFIGS: ClassVar[dict[str, dict[str, int | float | str]]] = {
        "Bias": {"minimum": -50, "maximum": 50, "step": 5, "unit": "%"},
        "Power": {"minimum": 0, "maximum": 2000, "step": 5, "unit": "W"},
        "Resistance": {"minimum": 0, "maximum": 100, "step": 1, "unit": "%"},
        "Speed": {"minimum": 0.0, "maximum": 15.0, "step": 0.1, "unit": "mph"},
    }

    def __init__(
        self,
        on_change,
        sport_type: SportTypesEnum,
        *,
        available_modes: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__()
        self._on_change = on_change
        if available_modes is None:
            available_modes = (
                ("Power", "Resistance") if sport_type == SportTypesEnum.biking else ("Speed",)
            )
        self.MODES = {mode: self.MODE_CONFIGS[mode] for mode in available_modes}
        self._mode = available_modes[0]
        self._values = {"Bias": 0, "Power": 100, "Resistance": 2, "Speed": 3.0}
        self._mode_buttons = {}

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for margin in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{margin}")(8)

        if len(available_modes) > 1:
            mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            mode_row.set_homogeneous(True)
            mode_row.set_hexpand(True)
            for mode in available_modes:
                button = Gtk.Button(label=mode)
                button.set_size_request(-1, 56)
                if mode == self._mode:
                    button.add_css_class("suggested-action")
                button.connect("clicked", self._on_mode_clicked, mode)
                self._mode_buttons[mode] = button
                mode_row.append(button)
            outer.append(mode_row)
        else:
            title = Gtk.Label(label=self._mode)
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

    def set_mode_available(self, mode: str, available: bool) -> None:
        """Show or hide an optional trainer control mode."""
        button = self._mode_buttons.get(mode)
        if button:
            button.set_visible(available)

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
        for button_mode, button in self._mode_buttons.items():
            button.set_css_classes(["suggested-action"] if button_mode == mode else [])
        self._refresh()
        self._on_change(self._mode, self._values[self._mode])

    def _refresh(self) -> None:
        config = self.MODES[self._mode]
        value = self._values[self._mode]
        value_text = f"{value:.1f}" if self._mode == "Speed" else str(value)
        self._lbl_value.set_text(f"{value_text} {config['unit']}")
        self._btn_down.set_sensitive(value > config["minimum"])
        self._btn_up.set_sensitive(value < config["maximum"])

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import gi

from fitness_tracker.core.trainer_mode import (
    TrainerMode,
    TrainerModeConfig,
    fallback_trainer_mode,
)
from fitness_tracker.core.units import (
    UnitSystem,
    mps_from_mph,
    speed_in_units,
    unit_label,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402  # ty:ignore[unresolved-import]


class TrainerTargetControl(Gtk.Frame):
    """Touch-friendly control for selecting and adjusting trainer targets."""

    MODE_CONFIGS: ClassVar[Mapping[TrainerMode, TrainerModeConfig]] = {
        TrainerMode.BIAS: TrainerModeConfig(
            minimum=-50,
            maximum=50,
            step=5,
            unit="%",
        ),
        TrainerMode.POWER: TrainerModeConfig(
            minimum=0,
            maximum=2000,
            step=5,
            unit="W",
        ),
        TrainerMode.RESISTANCE: TrainerModeConfig(
            minimum=0,
            maximum=100,
            step=1,
            unit="%",
        ),
    }
    _DEFAULT_SPEED_MPH: ClassVar[float] = 3.0
    _MAX_SPEED_MPH: ClassVar[float] = 15.0

    def __init__(
        self,
        on_change: Callable[[TrainerMode, float], None],
        *,
        available_modes: tuple[TrainerMode, ...],
        unit_system: UnitSystem = UnitSystem.IMPERIAL,
    ) -> None:
        super().__init__()
        self._on_change, self._unit_system = on_change, UnitSystem(unit_system)
        self._validate_modes(available_modes)
        self.MODES: dict[TrainerMode, TrainerModeConfig] = {
            mode: self._speed_config() if mode is TrainerMode.SPEED else self.MODE_CONFIGS[mode]
            for mode in available_modes
        }
        self._mode = available_modes[0]
        self._values = {
            TrainerMode.BIAS: 0.0,
            TrainerMode.POWER: 100.0,
            TrainerMode.RESISTANCE: 2.0,
            TrainerMode.SPEED: round(
                speed_in_units(mps_from_mph(self._DEFAULT_SPEED_MPH), self._unit_system),
                1,
            ),
        }
        self._mode_buttons = {}

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for margin in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{margin}")(8)

        if len(available_modes) > 1:
            mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            mode_row.set_homogeneous(True)
            mode_row.set_hexpand(True)
            for mode in available_modes:
                button = Gtk.Button(label=mode.value)
                button.set_size_request(-1, 56)
                if mode == self._mode:
                    button.add_css_class("suggested-action")
                button.connect("clicked", self._on_mode_clicked, mode)
                self._mode_buttons[mode] = button
                mode_row.append(button)
            outer.append(mode_row)
        else:
            title = Gtk.Label(label=self._mode.value)
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

    def _validate_modes(self, available_modes: tuple[TrainerMode, ...]) -> None:
        """Reject shared trainer modes that have no UI control configuration."""
        if not available_modes:
            message = "at least one trainer control mode is required"
            raise ValueError(message)
        for mode in available_modes:
            if mode is not TrainerMode.SPEED and mode not in self.MODE_CONFIGS:
                message = f"{mode} has no trainer control configuration"
                raise ValueError(message)

    def set_unit_system(self, unit_system: UnitSystem) -> None:
        """Update speed controls while preserving the selected physical target."""
        resolved_system = UnitSystem(unit_system)
        if resolved_system == self._unit_system:
            self._refresh()
            return

        speed_mps = None
        if TrainerMode.SPEED in self.MODES:
            speed_mps = self._values[TrainerMode.SPEED] / speed_in_units(
                1.0,
                self._unit_system,
            )
        self._unit_system = resolved_system
        if speed_mps is not None:
            self.MODES[TrainerMode.SPEED] = self._speed_config()
            self._values[TrainerMode.SPEED] = round(
                speed_mps * speed_in_units(1.0, self._unit_system),
                1,
            )
        self._refresh()

    def set_mode_available(self, mode: TrainerMode, *, available: bool) -> None:
        """Show or hide an optional trainer control mode."""
        button = self._mode_buttons.get(mode)
        if button:
            button.set_visible(available)
        if not available:
            fallback = fallback_trainer_mode(
                self._mode_buttons,
                current=self._mode,
                unavailable=mode,
            )
            if fallback is not None:
                self._set_mode(fallback)

    def _change(self, direction: int) -> None:
        config = self.MODES[self._mode]
        value = self._values[self._mode] + direction * config.step
        value = max(config.minimum, min(config.maximum, value))
        self._values[self._mode] = round(value, config.decimals)
        self._refresh()
        self._on_change(self._mode, self._values[self._mode])

    def _speed_config(self) -> TrainerModeConfig:
        """Return the speed control range in the selected display units."""
        return TrainerModeConfig(
            minimum=0.0,
            maximum=round(
                speed_in_units(
                    mps_from_mph(self._MAX_SPEED_MPH),
                    self._unit_system,
                ),
                1,
            ),
            step=0.1,
            unit=unit_label("speed", self._unit_system),
            decimals=1,
        )

    def _on_mode_clicked(self, _button: Gtk.Button, mode: TrainerMode) -> None:
        if mode == self._mode:
            return
        self._set_mode(mode)
        self._on_change(self._mode, self._values[self._mode])

    def _set_mode(self, mode: TrainerMode) -> None:
        """Select a mode without emitting a target-change callback."""
        self._mode = mode
        for button_mode, button in self._mode_buttons.items():
            button.set_css_classes(["suggested-action"] if button_mode == mode else [])
        self._refresh()

    def _refresh(self) -> None:
        config = self.MODES[self._mode]
        value = self._values[self._mode]
        value_text = f"{value:.{config.decimals}f}"
        self._lbl_value.set_text(f"{value_text} {config.unit}")
        self._btn_down.set_sensitive(value > config.minimum)
        self._btn_up.set_sensitive(value < config.maximum)

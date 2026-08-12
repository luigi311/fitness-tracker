"""Composable settings-page sections."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict, cast

import gi
from bleaksport import MachineType

from fitness_tracker.core.settings import (
    TRAINER_SUPPLIED_HR_LABEL,
    DatabaseSettings,
    DisplaySettings,
    IntervalsIcuAPI,
    PebbleSettings,
    PersonalSettings,
    SensorSettings,
    TrainerSettings,
)
from fitness_tracker.core.units import UnitSystem

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Adw, Gtk  # noqa: E402  # ty:ignore[unresolved-import]

NONE_LABEL = "None"


@dataclass(frozen=True)
class SensorRowSpec:
    """Declarative definition for one selectable sensor row."""

    key: str
    title: str
    scan_label: str
    scanner: Callable[[], None]
    scan_group: str
    settings_field: str


class TrainerDeviceInfo(TypedDict):
    """Discovered FTMS trainer metadata used by a trainer selector."""

    address: str | None
    machine_type: MachineType | int | None


type SensorAddressMap = dict[str, str]
type TrainerDeviceMap = dict[str, TrainerDeviceInfo]
type SectionMaps = dict[str, SensorAddressMap | TrainerDeviceMap]


@dataclass
class SensorRowWidgets:
    """Widgets owned by one selectable sensor row."""

    row: Adw.ActionRow
    spinner: Gtk.Spinner
    combo: Gtk.ComboBoxText
    scan_button: Gtk.Button


class SensorSection:
    """Build one declaratively specified sensor or trainer section."""

    def __init__(
        self,
        *,
        title: str,
        subtitle: str,
        settings: SensorSettings | TrainerSettings,
        specs: tuple[SensorRowSpec, ...],
    ) -> None:
        self.title = title
        self.subtitle = subtitle
        self.settings = settings
        self.specs = specs
        self.rows: dict[str, SensorRowWidgets] = {}
        self.maps: SectionMaps = {}

    def build(self) -> Adw.PreferencesGroup:
        """Build and return the preferences group for this section."""
        group = Adw.PreferencesGroup()
        group.set_title("")

        expander = Adw.ExpanderRow()
        expander.set_title(self.title)
        expander.set_subtitle(self.subtitle)
        expander.set_expanded(False)
        group.add(expander)

        for spec in self.specs:
            row = Adw.ActionRow()
            row.set_title(spec.title)
            spinner = Gtk.Spinner()
            combo = Gtk.ComboBoxText()
            combo.set_hexpand(True)
            row.add_prefix(spinner)
            row.add_suffix(combo)
            expander.add_row(row)

            scan_row = Adw.ActionRow()
            scan_button = Gtk.Button(label=spec.scan_label)
            scan_button.get_style_context().add_class("suggested-action")
            scan_button.connect(
                "clicked",
                lambda _button, scanner=spec.scanner: scanner(),
            )
            scan_row.add_suffix(scan_button)
            expander.add_row(scan_row)

            self.rows[spec.key] = SensorRowWidgets(
                row=row,
                spinner=spinner,
                combo=combo,
                scan_button=scan_button,
            )

        return group

    def row(self, key: str) -> SensorRowWidgets:
        """Return the widgets for a declared sensor key."""
        return self.rows[key]

    def scan_widgets(self, scan_group: str) -> tuple[SensorRowWidgets, ...]:
        """Return rows subscribed to one scanner group."""
        return tuple(self.rows[spec.key] for spec in self.specs if spec.scan_group == scan_group)

    def _active_name(self, spec: SensorRowSpec) -> str | None:
        if isinstance(self.settings, SensorSettings):
            match spec.settings_field:
                case "hr_name":
                    return self.settings.hr_name
                case "speed_name":
                    return self.settings.speed_name
                case "cadence_name":
                    return self.settings.cadence_name
                case "power_name":
                    return self.settings.power_name
                case _:
                    message = f"Unsupported sensor settings field: {spec.settings_field}"
                    raise ValueError(message)

        if spec.settings_field == "trainer_name":
            return self.settings.trainer_name
        if spec.settings_field == "hr_name":
            return (
                TRAINER_SUPPLIED_HR_LABEL
                if self.settings.trainer_supplied_hr
                else self.settings.hr_name
            )
        message = f"Unsupported trainer settings field: {spec.settings_field}"
        raise ValueError(message)

    def apply_scan_result(
        self,
        scan_group: str,
        mapping: SensorAddressMap | TrainerDeviceMap,
        empty_message: str | Mapping[str, str],
    ) -> None:
        """Apply one scanner result to every row subscribed by this section."""
        for spec in self.specs:
            if spec.scan_group != scan_group:
                continue
            names = sorted(mapping)
            if isinstance(self.settings, TrainerSettings) and spec.settings_field == "hr_name":
                names = [TRAINER_SUPPLIED_HR_LABEL, *names]
            row = self.rows[spec.key]
            status = (
                empty_message if isinstance(empty_message, str) else empty_message.get(spec.key, "")
            )
            row.row.set_subtitle("" if names else status)
            self._set_combo_items_with_none(row.combo, names, self._active_name(spec))
            self.maps[spec.key] = mapping

    @staticmethod
    def _set_combo_items_with_none(
        combo: Gtk.ComboBoxText,
        names: list[str],
        active_name: str | None,
    ) -> None:
        combo.remove_all()
        combo.append_text(NONE_LABEL)
        for name in names:
            combo.append_text(name)
        combo.set_active(names.index(active_name) + 1 if active_name in names else 0)

    def load(self) -> SectionMaps:
        """Load this section's settings into its combos and return device maps."""
        maps: SectionMaps = {}
        for spec in self.specs:
            row = self.rows[spec.key]
            if isinstance(self.settings, SensorSettings):
                match spec.settings_field:
                    case "hr_name":
                        name, address = self.settings.hr_name, self.settings.hr_address
                    case "speed_name":
                        name, address = self.settings.speed_name, self.settings.speed_address
                    case "cadence_name":
                        name, address = self.settings.cadence_name, self.settings.cadence_address
                    case "power_name":
                        name, address = self.settings.power_name, self.settings.power_address
                    case _:
                        message = f"Unsupported sensor settings field: {spec.settings_field}"
                        raise ValueError(message)
                self._set_combo_items_with_none(row.combo, [name] if name else [], name)
                maps[spec.key] = {name: cast("str", address)} if name else {}
                continue

            if spec.settings_field == "trainer_name":
                name = self.settings.trainer_name
                self._set_combo_items_with_none(row.combo, [name] if name else [], name)
                maps[spec.key] = (
                    {
                        name: {
                            "address": self.settings.trainer_address,
                            "machine_type": self.settings.trainer_machine_type,
                        },
                    }
                    if name
                    else {}
                )
                continue

            if spec.settings_field != "hr_name":
                message = f"Unsupported trainer settings field: {spec.settings_field}"
                raise ValueError(message)

            active_name = (
                TRAINER_SUPPLIED_HR_LABEL
                if self.settings.trainer_supplied_hr
                else self.settings.hr_name
            )
            names = [TRAINER_SUPPLIED_HR_LABEL]
            if self.settings.hr_name:
                names.append(self.settings.hr_name)
            self._set_combo_items_with_none(row.combo, names, active_name)
            maps[spec.key] = cast(
                "SensorAddressMap",
                {
                    self.settings.hr_name: self.settings.hr_address,
                }
                if self.settings.hr_name and not self.settings.trainer_supplied_hr
                else {},
            )
        self.maps = maps
        return maps

    def _selected_settings_values(
        self,
        maps: Mapping[
            str,
            Mapping[str, str | None] | Mapping[str, TrainerDeviceInfo],
        ]
        | None = None,
    ) -> dict[str, object]:
        """Return the selected device values without changing the live settings."""
        source_maps = self.maps if maps is None else maps
        values: dict[str, object] = {}
        for spec in self.specs:
            selected = self.rows[spec.key].combo.get_active_text()
            selected_name = None if selected == NONE_LABEL or not selected else selected
            device_map = source_maps.get(spec.key, {})

            if isinstance(self.settings, SensorSettings):
                address_map = cast("Mapping[str, str]", device_map)
                address = address_map.get(selected_name) if selected_name else None
                match spec.settings_field:
                    case "hr_name":
                        values.update(hr_name=selected_name, hr_address=address)
                    case "speed_name":
                        values.update(speed_name=selected_name, speed_address=address)
                    case "cadence_name":
                        values.update(cadence_name=selected_name, cadence_address=address)
                    case "power_name":
                        values.update(power_name=selected_name, power_address=address)
                    case _:
                        message = f"Unsupported sensor settings field: {spec.settings_field}"
                        raise ValueError(message)
                continue

            if spec.settings_field == "trainer_name":
                trainer_map = cast("Mapping[str, TrainerDeviceInfo]", device_map)
                info = trainer_map.get(selected_name) if selected_name else None
                values.update(
                    trainer_name=selected_name,
                    trainer_address=info["address"] if info else None,
                )
                machine_type = info["machine_type"] if info else None
                values["trainer_machine_type"] = (
                    machine_type.value if isinstance(machine_type, MachineType) else machine_type
                )
                continue

            if spec.settings_field != "hr_name":
                message = f"Unsupported trainer settings field: {spec.settings_field}"
                raise ValueError(message)

            if selected_name == TRAINER_SUPPLIED_HR_LABEL:
                values.update(trainer_supplied_hr=True, hr_name=None, hr_address=None)
                continue

            hr_map = cast("Mapping[str, str]", device_map)
            values.update(
                trainer_supplied_hr=False,
                hr_name=selected_name,
                hr_address=hr_map.get(selected_name) if selected_name else None,
            )
        return values

    def settings_data(
        self,
        maps: Mapping[
            str,
            Mapping[str, str | None] | Mapping[str, TrainerDeviceInfo],
        ]
        | None = None,
    ) -> dict[str, Any]:
        """Return complete validated-section input without changing live settings."""
        values = self.settings.model_dump(mode="python")
        values.update(self._selected_settings_values(maps))
        return values


class PersonalSection:
    """Build and read the personal-training preferences group."""

    def __init__(self, settings: PersonalSettings) -> None:
        self.settings = settings
        self.weight_spin: Gtk.SpinButton | None = None
        self.rest_spin: Gtk.SpinButton | None = None
        self.max_spin: Gtk.SpinButton | None = None
        self.lthr_spin: Gtk.SpinButton | None = None
        self.ftp_spin: Gtk.SpinButton | None = None

    def build(self) -> Adw.PreferencesGroup:
        """Build and return the personal-preferences group."""
        group = Adw.PreferencesGroup()
        group.set_title("Personal Info")
        self.weight_spin = self._add_spin(
            group,
            "Weight (kg)",
            30,
            225,
            self.settings.weight_kg,
        )
        self.rest_spin = self._add_spin(
            group,
            "Resting HR",
            30,
            120,
            self.settings.resting_hr,
        )
        self.max_spin = self._add_spin(
            group,
            "Max HR",
            100,
            250,
            self.settings.max_hr,
        )
        self.lthr_spin = self._add_spin(
            group,
            "Lactate Threshold HR",
            0,
            250,
            self.settings.lthr_bpm or 0,
            subtitle="Used for %LTHR workout targets; 0 disables",
        )
        self.ftp_spin = self._add_spin(
            group,
            "FTP (Watts)",
            50,
            2000,
            self.settings.ftp_watts,
        )
        return group

    @staticmethod
    def _add_spin(
        group: Adw.PreferencesGroup,
        title: str,
        minimum: int,
        maximum: int,
        value: float,
        *,
        subtitle: str | None = None,
    ) -> Gtk.SpinButton:
        row = Adw.ActionRow()
        row.set_title(title)
        if subtitle is not None:
            row.set_subtitle(subtitle)
        spin = Gtk.SpinButton.new_with_range(minimum, maximum, 1)
        spin.set_value(value)
        row.add_suffix(spin)
        group.add(row)
        return spin

    def settings_data(self) -> dict[str, float | int | None]:
        """Return validated personal values represented by the controls."""
        weight_spin = self.weight_spin
        rest_spin = self.rest_spin
        max_spin = self.max_spin
        lthr_spin = self.lthr_spin
        ftp_spin = self.ftp_spin
        if (
            weight_spin is None
            or rest_spin is None
            or max_spin is None
            or lthr_spin is None
            or ftp_spin is None
        ):
            message = "Personal settings controls have not been built"
            raise RuntimeError(message)
        return {
            "weight_kg": weight_spin.get_value_as_int(),
            "resting_hr": rest_spin.get_value_as_int(),
            "max_hr": max_spin.get_value_as_int(),
            "lthr_bpm": lthr_spin.get_value_as_int() or None,
            "ftp_watts": ftp_spin.get_value_as_int(),
        }


class DisplaySection:
    """Build and read display preferences."""

    def __init__(self, settings: DisplaySettings) -> None:
        self.settings = settings
        self.unit_system_combo: Gtk.ComboBoxText | None = None

    def build(self) -> Adw.PreferencesGroup:
        """Build and return the display-preferences group."""
        group = Adw.PreferencesGroup()
        group.set_title("Display")

        row = Adw.ActionRow()
        row.set_title("Unit system")
        combo = Gtk.ComboBoxText()
        combo.append_text("Metric")
        combo.append_text("Imperial")
        combo.set_active(0 if self.settings.unit_system is UnitSystem.METRIC else 1)
        row.add_suffix(combo)
        group.add(row)
        self.unit_system_combo = combo
        return group

    def settings_data(self) -> dict[str, UnitSystem]:
        """Return the selected display-unit preference."""
        if self.unit_system_combo is None:
            message = "Display settings controls have not been built"
            raise RuntimeError(message)
        selected = self.unit_system_combo.get_active_text()
        return {
            "unit_system": (UnitSystem.METRIC if selected == "Metric" else UnitSystem.IMPERIAL),
        }


class DevicesSection:
    """Compose the declarative sensor and trainer sections."""

    def __init__(self, sections: Mapping[str, SensorSection]) -> None:
        self.sections = dict(sections)

    def build(self) -> Adw.PreferencesGroup:
        """Build and return the device-preferences group."""
        group = Adw.PreferencesGroup()
        group.set_title("Devices")
        for section in self.sections.values():
            group.add(section.build())
        return group


class PebbleSection:
    """Build, populate, and read the Pebble preferences."""

    def __init__(self, settings: PebbleSettings, *, on_scan: Callable[[], None]) -> None:
        self.settings = settings
        self._on_scan = on_scan
        self.row: Adw.ActionRow | None = None
        self.enable_row: Adw.SwitchRow | None = None
        self.emu_switch: Adw.SwitchRow | None = None
        self.scan_row: Adw.ActionRow | None = None
        self.spinner: Gtk.Spinner | None = None
        self.combo: Gtk.ComboBoxText | None = None
        self.port_row: Adw.ActionRow | None = None
        self.port_spin: Gtk.SpinButton | None = None
        self.scan_button: Gtk.Button | None = None
        self.expander: Adw.ExpanderRow | None = None
        self.device_map: dict[str, str] = {}

    def build(self) -> Adw.PreferencesGroup:
        """Build and return the Pebble-preferences group."""
        group = Adw.PreferencesGroup()
        group.set_title("")

        enable_row = Adw.SwitchRow()
        enable_row.set_title("Enable Pebble")
        enable_row.set_active(self.settings.enable)
        group.add(enable_row)
        self.enable_row = enable_row

        expander = Adw.ExpanderRow()
        expander.set_title("Pebble Settings")
        expander.set_expanded(False)
        group.add(expander)
        self.expander = expander

        emu_switch = Adw.SwitchRow()
        emu_switch.set_title("Use Emulator")
        emu_switch.set_active(self.settings.use_emulator)
        expander.add_row(emu_switch)
        self.emu_switch = emu_switch

        self._build_device_row(expander)
        self._build_port_row(expander)
        self._build_scan_row(expander)

        enable_row.connect("notify::active", self._update_expander_state)
        emu_switch.connect("notify::active", self._on_mode_toggled)
        self._update_expander_state()
        self._on_mode_toggled(emu_switch)
        self.load()
        return group

    def _build_device_row(self, expander: Adw.ExpanderRow) -> None:
        row = Adw.ActionRow()
        row.set_title("Pebble")
        spinner = Gtk.Spinner()
        combo = Gtk.ComboBoxText()
        combo.set_hexpand(False)
        combo.set_size_request(240, -1)
        combo.set_halign(Gtk.Align.END)
        combo.connect("changed", self._on_combo_changed)
        row.add_prefix(spinner)
        row.add_suffix(combo)
        if hasattr(row, "set_title_lines"):
            row.set_title_lines(1)
        expander.add_row(row)
        self.row = row
        self.spinner = spinner
        self.combo = combo

    def _build_port_row(self, expander: Adw.ExpanderRow) -> None:
        port_row = Adw.ActionRow()
        port_row.set_title("Emulator Port")
        port_spin = Gtk.SpinButton.new_with_range(1, 65535, 1)
        port_spin.set_value(self.settings.port or 47527)
        port_spin.set_hexpand(False)
        port_spin.set_width_chars(6)
        port_row.add_suffix(port_spin)
        expander.add_row(port_row)
        self.port_row = port_row
        self.port_spin = port_spin

    def _build_scan_row(self, expander: Adw.ExpanderRow) -> None:
        scan_row = Adw.ActionRow()
        scan_button = Gtk.Button(label="Scan Pebble")
        scan_button.get_style_context().add_class("suggested-action")
        scan_button.connect("clicked", lambda _button: self._on_scan())
        scan_row.add_suffix(scan_button)
        expander.add_row(scan_row)
        self.scan_row = scan_row
        self.scan_button = scan_button

    def _update_expander_state(self, *_args: object) -> None:
        if self.enable_row is None or self.expander is None:
            return
        enabled = bool(self.enable_row.get_active())
        self.expander.set_sensitive(enabled)
        if not enabled:
            self.expander.set_expanded(False)
        self.expander.set_subtitle("Enabled" if enabled else "Disabled")

    def _on_mode_toggled(self, switch: Adw.SwitchRow, _pspec: object | None = None) -> bool:
        use_emu = switch.get_active()
        if self.row is not None:
            self.row.set_visible(not use_emu)
            self.row.set_subtitle("Emulator mode" if use_emu else "")
        if self.scan_row is not None:
            self.scan_row.set_visible(not use_emu)
        if self.port_row is not None:
            self.port_row.set_visible(use_emu)
        return False

    def _on_combo_changed(self, _combo: Gtk.ComboBoxText) -> None:
        if self.combo is None or not self.device_map:
            return
        display_name = self.combo.get_active_text() or ""
        self.combo.set_tooltip_text(self.device_map.get(display_name) or None)

    def load(self) -> None:
        """Populate controls from the current Pebble settings."""
        if self.settings.use_emulator and self.row is not None:
            self.row.set_subtitle("Emulator mode")
        if self.settings.name and self.combo is not None:
            self.combo.append_text(self.settings.name)
            self.combo.set_active(0)
            self.device_map = {self.settings.name: self.settings.address or ""}

    def set_settings(self, settings: PebbleSettings) -> None:
        """Use a newly saved settings model for future scans and loads."""
        self.settings = settings

    def set_scan_started(self) -> None:
        """Show that a Pebble scan is in progress."""
        if self.spinner is not None:
            self.spinner.start()
        if self.row is not None:
            self.row.set_subtitle("Scanning for Pebble…")

    def set_scan_finished(self) -> None:
        """Stop the Pebble scan indicator."""
        if self.spinner is not None:
            self.spinner.stop()

    def set_scan_error(self, message: str) -> None:
        """Display a Pebble scan failure in the selector row."""
        if self.row is not None:
            self.row.set_subtitle(message)

    def set_scan_results(self, name_to_mac: Mapping[str, str]) -> None:
        """Replace the selector contents with a scan result."""
        if self.combo is None or self.row is None:
            return
        display_map = self._unique_display_names(name_to_mac)
        names = sorted(display_map)
        self.combo.remove_all()
        for display_name in names:
            self.combo.append_text(display_name)
        if not names:
            self.row.set_subtitle("No Pebble devices found")
        else:
            self.row.set_subtitle("")
            if self.settings.address:
                for index, display_name in enumerate(names):
                    if display_map[display_name] == self.settings.address:
                        self.combo.set_active(index)
                        break
        self.device_map = display_map

    @staticmethod
    def _unique_display_names(name_to_mac: Mapping[str, str]) -> dict[str, str]:
        """Make duplicate device names unambiguous in the selector."""
        counts: dict[str, int] = {}
        for name in name_to_mac:
            counts[name] = counts.get(name, 0) + 1
        seen_indices: dict[str, int] = {}
        display_map: dict[str, str] = {}
        for name, mac in name_to_mac.items():
            if counts[name] == 1:
                display_name = name
            else:
                index = seen_indices.get(name, 0) + 1
                seen_indices[name] = index
                display_name = f"{name} ({index})"
            display_map[display_name] = mac
        return display_map

    def settings_data(self) -> dict[str, object]:
        """Return the Pebble values represented by the controls."""
        if self.enable_row is None or self.emu_switch is None or self.combo is None:
            message = "Pebble settings controls have not been built"
            raise RuntimeError(message)
        use_emulator = self.emu_switch.get_active()
        display_name = self.combo.get_active_text() or ""
        return {
            "enable": self.enable_row.get_active(),
            "use_emulator": use_emulator,
            "port": self.port_spin.get_value_as_int() if self.port_spin else self.settings.port,
            "name": None if use_emulator else display_name or None,
            "address": None if use_emulator else self.device_map.get(display_name),
        }


class ProviderSection:
    """Build and read Intervals.icu and database provider settings."""

    def __init__(
        self,
        icu_settings: IntervalsIcuAPI,
        database_settings: DatabaseSettings,
    ) -> None:
        self.icu_settings = icu_settings
        self.database_settings = database_settings
        self.icu_id_entry: Gtk.Entry | None = None
        self.icu_key_entry: Gtk.Entry | None = None
        self.dsn_entry: Gtk.Entry | None = None
        self._icu_expander: Adw.ExpanderRow | None = None
        self._database_expander: Adw.ExpanderRow | None = None

    def build(self) -> Adw.PreferencesGroup:
        """Build and return the provider-preferences group."""
        group = Adw.PreferencesGroup()
        group.set_title("Data Providers")

        icu_group = Adw.PreferencesGroup()
        icu_group.set_title("")
        icu_expander = Adw.ExpanderRow()
        icu_expander.set_title("Intervals.icu")
        icu_expander.set_expanded(False)
        icu_group.add(icu_expander)
        self._icu_expander = icu_expander

        row_icu_id = Adw.ActionRow()
        row_icu_id.set_title("Athlete ID")
        self.icu_id_entry = Gtk.Entry()
        self.icu_id_entry.set_hexpand(True)
        self.icu_id_entry.set_text(self.icu_settings.athlete_id or "")
        row_icu_id.add_suffix(self.icu_id_entry)
        icu_expander.add_row(row_icu_id)

        row_icu_key = Adw.ActionRow()
        row_icu_key.set_title("API Key")
        self.icu_key_entry = Gtk.Entry()
        self.icu_key_entry.set_visibility(False)
        self.icu_key_entry.set_hexpand(True)
        self.icu_key_entry.set_text(self.icu_settings.api_key or "")
        row_icu_key.add_suffix(self.icu_key_entry)
        icu_expander.add_row(row_icu_key)

        self.icu_id_entry.connect("changed", self._update_icu_subtitle)
        self.icu_key_entry.connect("changed", self._update_icu_subtitle)
        self._update_icu_subtitle()

        database_group = Adw.PreferencesGroup()
        database_group.set_title("")
        database_expander = Adw.ExpanderRow()
        database_expander.set_title("Database")
        database_expander.set_expanded(False)
        database_group.add(database_expander)
        self._database_expander = database_expander

        dsn_row = Adw.ActionRow()
        dsn_row.set_title("Database DSN")
        self.dsn_entry = Gtk.Entry()
        self.dsn_entry.set_hexpand(True)
        self.dsn_entry.set_text(self.database_settings.dsn or "")
        dsn_row.add_suffix(self.dsn_entry)
        database_expander.add_row(dsn_row)
        self.dsn_entry.connect("changed", self._update_database_subtitle)
        self._update_database_subtitle()

        group.add(icu_group)
        group.add(database_group)
        return group

    def _update_icu_subtitle(self, *_args: object) -> None:
        if self._icu_expander is None:
            return
        athlete_id = self.icu_id_entry.get_text().strip() if self.icu_id_entry else ""
        api_key = self.icu_key_entry.get_text().strip() if self.icu_key_entry else ""
        self._icu_expander.set_subtitle(
            "Configured" if athlete_id and api_key else "Not configured",
        )

    def _update_database_subtitle(self, *_args: object) -> None:
        if self._database_expander is None:
            return
        dsn = self.dsn_entry.get_text().strip() if self.dsn_entry else ""
        self._database_expander.set_subtitle("Configured" if dsn else "Not configured")

    def set_settings(
        self,
        icu_settings: IntervalsIcuAPI,
        database_settings: DatabaseSettings,
    ) -> None:
        """Use newly saved provider models for future form loads."""
        self.icu_settings = icu_settings
        self.database_settings = database_settings

    def settings_data(self) -> dict[str, dict[str, str | None]]:
        """Return provider values represented by the controls."""
        return {
            "icu": {
                "athlete_id": self.icu_id_entry.get_text().strip() if self.icu_id_entry else None,
                "api_key": self.icu_key_entry.get_text().strip() if self.icu_key_entry else None,
            },
            "database": {
                "dsn": self.dsn_entry.get_text().strip() if self.dsn_entry else None,
            },
        }

    @property
    def icu_configured(self) -> bool:
        """Whether the current Intervals.icu form has both credentials."""
        return bool(
            self.icu_id_entry
            and self.icu_key_entry
            and self.icu_id_entry.get_text().strip()
            and self.icu_key_entry.get_text().strip(),
        )

    @property
    def database_configured(self) -> bool:
        """Whether the current database form has a DSN."""
        return bool(self.dsn_entry and self.dsn_entry.get_text().strip())


class ActionSection:
    """Build settings actions and expose their state controls."""

    def __init__(
        self,
        *,
        icu_configured: bool,
        database_configured: bool,
        on_save: Callable[..., None],
        on_fetch: Callable[..., None],
        on_upload: Callable[..., None],
        on_sync: Callable[..., None],
    ) -> None:
        self.icu_configured = icu_configured
        self.database_configured = database_configured
        self.on_save = on_save
        self.on_fetch = on_fetch
        self.on_upload = on_upload
        self.on_sync = on_sync
        self.row_fetch: Adw.ActionRow | None = None
        self.row_upload: Adw.ActionRow | None = None
        self.row_sync: Adw.ActionRow | None = None
        self.save_button: Gtk.Button | None = None
        self.fetch_button: Gtk.Button | None = None
        self.upload_button: Gtk.Button | None = None
        self.sync_button: Gtk.Button | None = None

    def build(self) -> Adw.PreferencesGroup:
        """Build and return the settings-actions group."""
        group = Adw.PreferencesGroup()
        group.set_title("Actions")

        save_row = Adw.ActionRow()
        save_row.set_title("Save Settings")
        save_row.set_activatable(True)
        self.save_button = Gtk.Button(label="Save")
        self.save_button.get_style_context().add_class("suggested-action")
        self.save_button.connect("clicked", self.on_save)
        save_row.add_suffix(self.save_button)
        group.add(save_row)

        self.row_fetch, self.fetch_button = self._button_row(
            group,
            "Fetch Intervals.icu week",
            "Fetch",
            self.on_fetch,
            enabled=self.icu_configured,
        )
        self.row_upload, self.upload_button = self._button_row(
            group,
            "Upload to Intervals.icu",
            "Upload",
            self.on_upload,
            enabled=self.icu_configured,
        )
        self.row_sync, self.sync_button = self._button_row(
            group,
            "Sync to Database",
            "Sync",
            self.on_sync,
            enabled=self.database_configured,
        )
        return group

    @staticmethod
    def _button_row(
        group: Adw.PreferencesGroup,
        title: str,
        label: str,
        callback: Callable[..., None],
        *,
        enabled: bool,
    ) -> tuple[Adw.ActionRow, Gtk.Button]:
        row = Adw.ActionRow()
        row.set_title(title)
        row.set_activatable(enabled)
        button = Gtk.Button(label=label)
        button.get_style_context().add_class("suggested-action")
        button.connect("clicked", callback)
        row.add_suffix(button)
        group.add(row)
        return row, button

    def set_enabled(self, *, icu_configured: bool, database_configured: bool) -> None:
        """Update action availability from the current form values."""
        self._set_action_enabled(
            self.row_fetch,
            self.fetch_button,
            enabled=icu_configured,
        )
        self._set_action_enabled(
            self.row_upload,
            self.upload_button,
            enabled=icu_configured,
        )
        self._set_action_enabled(
            self.row_sync,
            self.sync_button,
            enabled=database_configured,
        )

    @staticmethod
    def _set_action_enabled(
        row: Adw.ActionRow | None,
        button: Gtk.Button | None,
        *,
        enabled: bool,
    ) -> None:
        if row is None or button is None:
            return
        button.set_sensitive(enabled)
        row.set_sensitive(enabled)
        context = button.get_style_context()
        if enabled:
            context.add_class("suggested-action")
        else:
            context.remove_class("suggested-action")

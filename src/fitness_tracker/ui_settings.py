import asyncio
import contextlib
import subprocess
import threading
from configparser import ConfigParser
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import gi
import requests
from bleak import BleakScanner
from bleaksport import (
    MachineType,
    discover_ftms_devices,
    discover_heart_rate_devices,
    discover_power_devices,
    discover_speed_cadence_devices,
)
from loguru import logger
from pydantic import BaseModel
from pydantic_file_settings import FileSettings
from pydantic_settings import SettingsConfigDict

from fitness_tracker import upload_providers, workout_providers
from fitness_tracker.database import SportTypesEnum

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Adw, GLib, Gtk  # noqa: E402  # ty:ignore[unresolved-import]

NONE_LABEL = "None"


class PersonalSettings(BaseModel):
    weight_kg: float = 80.0
    resting_hr: int = 60
    max_hr: int = 200
    ftp_watts: int = 150


class SensorSettings(BaseModel):
    hr_name: str | None = None
    hr_address: str | None = None
    speed_name: str | None = None
    speed_address: str | None = None
    cadence_name: str | None = None
    cadence_address: str | None = None
    power_name: str | None = None
    power_address: str | None = None


class TrainerSettings(BaseModel):
    hr_name: str | None = None
    hr_address: str | None = None
    trainer_name: str | None = None
    trainer_address: str | None = None
    trainer_machine_type: MachineType | None = None


class PebbleSettings(BaseModel):
    enable: bool = False
    uuid: str = "f4fcdac7-f58e-4d22-96bd-48cf98e25d09"
    use_emulator: bool = False
    port: int = 47527
    name: str | None = None
    address: str | None = None


class IntervalsIcuAPI(BaseModel):
    athlete_id: str | None = None
    api_key: str | None = None


class DatabaseSettings(BaseModel):
    dsn: str | None = None


class AppSettings(FileSettings):
    model_config = SettingsConfigDict(nested_model_default_partial_update=True)

    personal: PersonalSettings = PersonalSettings()
    running_sensors: SensorSettings = SensorSettings()
    cycling_sensors: SensorSettings = SensorSettings()
    trainer_running: TrainerSettings = TrainerSettings()
    trainer_cycling: TrainerSettings = TrainerSettings()
    pebble: PebbleSettings = PebbleSettings()
    icu: IntervalsIcuAPI = IntervalsIcuAPI()
    database: DatabaseSettings = DatabaseSettings()


def fallback_settings(file: Path) -> AppSettings | None:
    if not file.exists():
        return None

    logger.debug(f"Found old config file at {file}, attempting to migrate settings")
    cfg = ConfigParser()
    cfg.read(file)
    database_dsn = cfg.get("server", "database_dsn", fallback="")

    # Sensors Running
    hr_name = cfg.get("sensors_running", "hr_name", fallback="")
    hr_address = cfg.get("sensors_running", "hr_address", fallback="")
    speed_name = cfg.get("sensors_running", "speed_name", fallback="")
    speed_address = cfg.get("sensors_running", "speed_address", fallback="")
    cadence_name = cfg.get("sensors_running", "cadence_name", fallback="")
    cadence_address = cfg.get("sensors_running", "cadence_address", fallback="")
    power_name = cfg.get("sensors_running", "power_name", fallback="")
    power_address = cfg.get("sensors_running", "power_address", fallback="")

    # Sensors Cycling
    cycling_hr_name = cfg.get("sensors_cycling", "hr_name", fallback="")
    cycling_hr_address = cfg.get("sensors_cycling", "hr_address", fallback="")
    cycling_speed_name = cfg.get("sensors_cycling", "speed_name", fallback="")
    cycling_speed_address = cfg.get("sensors_cycling", "speed_address", fallback="")
    cycling_cadence_name = cfg.get("sensors_cycling", "cadence_name", fallback="")
    cycling_cadence_address = cfg.get("sensors_cycling", "cadence_address", fallback="")
    cycling_power_name = cfg.get("sensors_cycling", "power_name", fallback="")
    cycling_power_address = cfg.get("sensors_cycling", "power_address", fallback="")

    # Trainer (FTMS)
    trainer_running_hr_name = cfg.get("sensors_trainer_running", "hr_name", fallback="")
    trainer_running_hr_address = cfg.get("sensors_trainer_running", "hr_address", fallback="")
    trainer_running_name = cfg.get("sensors_trainer_running", "trainer_name", fallback="")
    trainer_running_address = cfg.get("sensors_trainer_running", "trainer_address", fallback="")

    trainer_running_machine_type = None
    trainer_running_machine_type_str = cfg.get(
        "sensors_trainer_running",
        "trainer_machine_type",
        fallback="",
    )
    if trainer_running_machine_type_str:
        trainer_running_machine_type = MachineType(int(trainer_running_machine_type_str))
    trainer_cycling_hr_name = cfg.get("sensors_trainer_cycling", "hr_name", fallback="")
    trainer_cycling_hr_address = cfg.get("sensors_trainer_cycling", "hr_address", fallback="")
    trainer_cycling_name = cfg.get("sensors_trainer_cycling", "trainer_name", fallback="")
    trainer_cycling_address = cfg.get("sensors_trainer_cycling", "trainer_address", fallback="")

    trainer_cycling_machine_type = None
    trainer_cycling_machine_type_str = cfg.get(
        "sensors_trainer_cycling",
        "trainer_machine_type",
        fallback="",
    )
    if trainer_cycling_machine_type_str:
        trainer_cycling_machine_type = MachineType(int(trainer_cycling_machine_type_str))
    weight_kg = cfg.getint("personal", "weight_kg", fallback=80)
    resting_hr = cfg.getint("personal", "resting_hr", fallback=60)
    max_hr = cfg.getint("personal", "max_hr", fallback=180)
    ftp_watts = cfg.getint("personal", "ftp_watts", fallback=150)

    # Pebble device
    pebble_enable = cfg.getboolean(
        "pebble",
        "enable",
        fallback=False,
    )
    pebble_use_emulator = cfg.getboolean(
        "pebble",
        "use_emulator",
        fallback=False,
    )
    pebble_name = cfg.get("pebble", "name", fallback=None)
    pebble_address = cfg.get("pebble", "mac", fallback=None)
    pebble_port = cfg.getint("pebble", "port", fallback=47527)

    # Intervals.icu
    icu_athlete_id = cfg.get("intervals_icu", "athlete_id", fallback="")
    icu_api_key = cfg.get("intervals_icu", "api_key", fallback="")

    return AppSettings(
        personal=PersonalSettings(
            weight_kg=weight_kg,
            resting_hr=resting_hr,
            max_hr=max_hr,
            ftp_watts=ftp_watts,
        ),
        running_sensors=SensorSettings(
            hr_name=hr_name,
            hr_address=hr_address,
            speed_name=speed_name,
            speed_address=speed_address,
            cadence_name=cadence_name,
            cadence_address=cadence_address,
            power_name=power_name,
            power_address=power_address,
        ),
        cycling_sensors=SensorSettings(
            hr_name=cycling_hr_name,
            hr_address=cycling_hr_address,
            speed_name=cycling_speed_name,
            speed_address=cycling_speed_address,
            cadence_name=cycling_cadence_name,
            cadence_address=cycling_cadence_address,
            power_name=cycling_power_name,
            power_address=cycling_power_address,
        ),
        trainer_running=TrainerSettings(
            hr_name=trainer_running_hr_name,
            hr_address=trainer_running_hr_address,
            trainer_name=trainer_running_name,
            trainer_address=trainer_running_address,
            trainer_machine_type=trainer_running_machine_type,
        ),
        trainer_cycling=TrainerSettings(
            hr_name=trainer_cycling_hr_name,
            hr_address=trainer_cycling_hr_address,
            trainer_name=trainer_cycling_name,
            trainer_address=trainer_cycling_address,
            trainer_machine_type=trainer_cycling_machine_type,
        ),
        pebble=PebbleSettings(
            enable=pebble_enable,
            use_emulator=pebble_use_emulator,
            name=pebble_name,
            address=pebble_address,
            port=pebble_port,
        ),
        icu=IntervalsIcuAPI(athlete_id=icu_athlete_id, api_key=icu_api_key),
        database=DatabaseSettings(
            dsn=database_dsn,
        ),
    )


class SettingsPageUI:
    def __init__(self, app):
        self.app = app

        # Widgets to toggle
        self.pebble_row: Adw.ActionRow | None = None
        self.pebble_enable_row: Adw.SwitchRow | None = None
        self.pebble_emu_switch: Adw.SwitchRow | None = None
        self.pebble_scan_row: Adw.ActionRow | None = None
        self.pebble_spinner: Gtk.Spinner | None = None
        self.pebble_combo: Gtk.ComboBoxText | None = None
        self.pebble_port_row: Adw.ActionRow | None = None
        self.pebble_port_spin: Gtk.SpinButton | None = None

        # Sensors
        self.hr_map: dict[str, str] = {}
        self.speed_map: dict[str, str] = {}
        self.cadence_map: dict[str, str] = {}
        self.power_map: dict[str, str] = {}

        # Trainer (FTMS): display -> {"address": str, "machine_type": MachineType}
        self.trainer_cycling_hr_map: dict[str, str] = {}
        self.trainer_cycling_map: dict[str, dict[str, MachineType]] = {}
        self.trainer_running_hr_map: dict[str, str] = {}
        self.trainer_running_map: dict[str, dict[str, MachineType]] = {}

        # pebble
        self.pebble_map: dict[str, str] = {}

        # Intervals ICU
        self.icu_id_entry = None
        self.icu_key_entry = None
        self.btn_fetch_icu = None
        self.btn_upload_icu = None

    def _combo_set_items_with_none(
        self,
        combo: Gtk.ComboBoxText,
        names: list[str],
        active_name: str | None,
    ):
        combo.remove_all()
        combo.append_text(NONE_LABEL)
        for n in names:
            combo.append_text(n)

        # Select saved name if present, else select None
        if active_name and active_name in names:
            combo.set_active(names.index(active_name) + 1)
        else:
            combo.set_active(0)

    def build_page(self) -> Gtk.Widget:
        # Outer scroller so the page never overflows vertically
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)

        # ----- Personal settings group -----
        personal_group = Adw.PreferencesGroup()
        personal_group.set_title("Personal Info")

        # Weight
        weight_row = Adw.ActionRow()
        weight_row.set_title("Weight (kg)")
        self.weight_spin = Gtk.SpinButton.new_with_range(30, 225, 1)
        self.weight_spin.set_value(self.app.app_settings.personal.weight_kg)
        weight_row.add_suffix(self.weight_spin)
        personal_group.add(weight_row)

        # Resting HR
        rest_row = Adw.ActionRow()
        rest_row.set_title("Resting HR")
        self.rest_spin = Gtk.SpinButton.new_with_range(30, 120, 1)
        self.rest_spin.set_value(self.app.app_settings.personal.resting_hr)
        rest_row.add_suffix(self.rest_spin)
        personal_group.add(rest_row)

        # Max HR
        max_row = Adw.ActionRow()
        max_row.set_title("Max HR")
        self.max_spin = Gtk.SpinButton.new_with_range(100, 250, 1)
        self.max_spin.set_value(self.app.app_settings.personal.max_hr)
        max_row.add_suffix(self.max_spin)
        personal_group.add(max_row)

        # FTP (for workouts)
        ftp_row = Adw.ActionRow()
        ftp_row.set_title("FTP (Watts)")
        self.ftp_spin = Gtk.SpinButton.new_with_range(50, 2000, 1)
        self.ftp_spin.set_value(self.app.app_settings.personal.ftp_watts)
        ftp_row.add_suffix(self.ftp_spin)
        personal_group.add(ftp_row)

        # ----- Devices group -----
        devices_group = Adw.PreferencesGroup()
        devices_group.set_title("Devices")

        # ----- Sensors group -----
        # ----- Running Sensors -----
        sensors_running_group = Adw.PreferencesGroup()
        sensors_running_group.set_title("")

        sensors_running_expander = Adw.ExpanderRow()
        sensors_running_expander.set_title("Running Sensors")
        sensors_running_expander.set_subtitle("Heart rate, speed, cadence, power")
        sensors_running_expander.set_expanded(False)
        sensors_running_group.add(sensors_running_expander)

        # Heart Rate Monitor
        self.hr_row = Adw.ActionRow()
        self.hr_row.set_title("Select HRM")
        self.hr_spinner = Gtk.Spinner()
        self.hr_combo = Gtk.ComboBoxText()
        self.hr_combo.set_hexpand(True)
        self.hr_row.add_prefix(self.hr_spinner)
        self.hr_row.add_suffix(self.hr_combo)
        sensors_running_expander.add_row(self.hr_row)

        hr_scan_row = Adw.ActionRow()
        self.hr_scan_button = Gtk.Button(label="Scan HRM")
        self.hr_scan_button.get_style_context().add_class("suggested-action")
        self.hr_scan_button.connect(
            "clicked",
            lambda _: threading.Thread(target=self._fill_devices_hr, daemon=True).start(),
        )
        hr_scan_row.add_suffix(self.hr_scan_button)
        sensors_running_expander.add_row(hr_scan_row)

        # Speed
        self.speed_row = Adw.ActionRow()
        self.speed_row.set_title("Select Speed Device")
        self.speed_spinner = Gtk.Spinner()
        self.speed_combo = Gtk.ComboBoxText()
        self.speed_combo.set_hexpand(True)
        self.speed_row.add_prefix(self.speed_spinner)
        self.speed_row.add_suffix(self.speed_combo)
        sensors_running_expander.add_row(self.speed_row)

        speed_scan_row = Adw.ActionRow()
        self.speed_scan_button = Gtk.Button(label="Scan Speed")
        self.speed_scan_button.get_style_context().add_class("suggested-action")
        self.speed_scan_button.connect(
            "clicked",
            lambda _: threading.Thread(
                target=self._fill_devices_speed_cadence, daemon=True
            ).start(),
        )
        speed_scan_row.add_suffix(self.speed_scan_button)
        sensors_running_expander.add_row(speed_scan_row)

        # Cadence
        self.cadence_row = Adw.ActionRow()
        self.cadence_row.set_title("Select Cadence Device")
        self.cadence_spinner = Gtk.Spinner()
        self.cadence_combo = Gtk.ComboBoxText()
        self.cadence_combo.set_hexpand(True)
        self.cadence_row.add_prefix(self.cadence_spinner)
        self.cadence_row.add_suffix(self.cadence_combo)
        sensors_running_expander.add_row(self.cadence_row)

        cadence_scan_row = Adw.ActionRow()
        self.cadence_scan_button = Gtk.Button(label="Scan Cadence")
        self.cadence_scan_button.get_style_context().add_class("suggested-action")
        self.cadence_scan_button.connect(
            "clicked",
            lambda _: threading.Thread(
                target=self._fill_devices_speed_cadence, daemon=True
            ).start(),
        )
        cadence_scan_row.add_suffix(self.cadence_scan_button)
        sensors_running_expander.add_row(cadence_scan_row)

        # Power
        self.power_row = Adw.ActionRow()
        self.power_row.set_title("Select Power Device")
        self.power_spinner = Gtk.Spinner()
        self.power_combo = Gtk.ComboBoxText()
        self.power_combo.set_hexpand(True)
        self.power_row.add_prefix(self.power_spinner)
        self.power_row.add_suffix(self.power_combo)
        sensors_running_expander.add_row(self.power_row)

        power_scan_row = Adw.ActionRow()
        self.power_scan_button = Gtk.Button(label="Scan Power")
        self.power_scan_button.get_style_context().add_class("suggested-action")
        self.power_scan_button.connect(
            "clicked",
            lambda _: threading.Thread(target=self._fill_devices_power, daemon=True).start(),
        )
        power_scan_row.add_suffix(self.power_scan_button)
        sensors_running_expander.add_row(power_scan_row)

        devices_group.add(sensors_running_group)

        # ----- Cycling Sensors -----
        sensors_cycling_group = Adw.PreferencesGroup()
        sensors_cycling_group.set_title("")

        sensors_cycling_expander = Adw.ExpanderRow()
        sensors_cycling_expander.set_title("Cycling Sensors")
        sensors_cycling_expander.set_subtitle("Heart rate, speed, cadence, power")
        sensors_cycling_expander.set_expanded(False)
        sensors_cycling_group.add(sensors_cycling_expander)

        # Heart Rate Monitor
        self.cycling_hr_row = Adw.ActionRow()
        self.cycling_hr_row.set_title("Select HRM")
        self.cycling_hr_spinner = Gtk.Spinner()
        self.cycling_hr_combo = Gtk.ComboBoxText()
        self.cycling_hr_combo.set_hexpand(True)
        self.cycling_hr_row.add_prefix(self.cycling_hr_spinner)
        self.cycling_hr_row.add_suffix(self.cycling_hr_combo)
        sensors_cycling_expander.add_row(self.cycling_hr_row)

        cycling_hr_scan_row = Adw.ActionRow()
        self.cycling_hr_scan_button = Gtk.Button(label="Scan HRM")
        self.cycling_hr_scan_button.get_style_context().add_class("suggested-action")
        self.cycling_hr_scan_button.connect(
            "clicked",
            lambda _: threading.Thread(target=self._fill_devices_hr, daemon=True).start(),
        )
        cycling_hr_scan_row.add_suffix(self.cycling_hr_scan_button)
        sensors_cycling_expander.add_row(cycling_hr_scan_row)

        # Speed
        self.cycling_speed_row = Adw.ActionRow()
        self.cycling_speed_row.set_title("Select Speed Device")
        self.cycling_speed_spinner = Gtk.Spinner()
        self.cycling_speed_combo = Gtk.ComboBoxText()
        self.cycling_speed_combo.set_hexpand(True)
        self.cycling_speed_row.add_prefix(self.cycling_speed_spinner)
        self.cycling_speed_row.add_suffix(self.cycling_speed_combo)
        sensors_cycling_expander.add_row(self.cycling_speed_row)

        cycling_speed_scan_row = Adw.ActionRow()
        self.cycling_speed_scan_button = Gtk.Button(label="Scan Speed")
        self.cycling_speed_scan_button.get_style_context().add_class("suggested-action")
        self.cycling_speed_scan_button.connect(
            "clicked",
            lambda _: threading.Thread(
                target=self._fill_devices_speed_cadence, daemon=True
            ).start(),
        )
        cycling_speed_scan_row.add_suffix(self.cycling_speed_scan_button)
        sensors_cycling_expander.add_row(cycling_speed_scan_row)

        # Cadence
        self.cycling_cadence_row = Adw.ActionRow()
        self.cycling_cadence_row.set_title("Select Cadence Device")
        self.cycling_cadence_spinner = Gtk.Spinner()
        self.cycling_cadence_combo = Gtk.ComboBoxText()
        self.cycling_cadence_combo.set_hexpand(True)
        self.cycling_cadence_row.add_prefix(self.cycling_cadence_spinner)
        self.cycling_cadence_row.add_suffix(self.cycling_cadence_combo)
        sensors_cycling_expander.add_row(self.cycling_cadence_row)
        cycling_cadence_scan_row = Adw.ActionRow()
        self.cycling_cadence_scan_button = Gtk.Button(label="Scan Cadence")
        self.cycling_cadence_scan_button.get_style_context().add_class("suggested-action")
        self.cycling_cadence_scan_button.connect(
            "clicked",
            lambda _: threading.Thread(
                target=self._fill_devices_speed_cadence, daemon=True
            ).start(),
        )
        cycling_cadence_scan_row.add_suffix(self.cycling_cadence_scan_button)
        sensors_cycling_expander.add_row(cycling_cadence_scan_row)

        # Power
        self.cycling_power_row = Adw.ActionRow()
        self.cycling_power_row.set_title("Select Power Device")
        self.cycling_power_spinner = Gtk.Spinner()
        self.cycling_power_combo = Gtk.ComboBoxText()
        self.cycling_power_combo.set_hexpand(True)
        self.cycling_power_row.add_prefix(self.cycling_power_spinner)
        self.cycling_power_row.add_suffix(self.cycling_power_combo)
        sensors_cycling_expander.add_row(self.cycling_power_row)

        cycling_power_scan_row = Adw.ActionRow()
        self.cycling_power_scan_button = Gtk.Button(label="Scan Power")
        self.cycling_power_scan_button.get_style_context().add_class("suggested-action")
        self.cycling_power_scan_button.connect(
            "clicked",
            lambda _: threading.Thread(target=self._fill_devices_power, daemon=True).start(),
        )
        cycling_power_scan_row.add_suffix(self.cycling_power_scan_button)
        sensors_cycling_expander.add_row(cycling_power_scan_row)

        devices_group.add(sensors_cycling_group)

        # ----- Trainer (FTMS) -----
        trainer_group = Adw.PreferencesGroup()
        trainer_group.set_title("")

        trainer_expander = Adw.ExpanderRow()
        trainer_expander.set_title("Trainer (FTMS)")
        trainer_expander.set_subtitle("Smart trainer / indoor bike / treadmill")
        trainer_expander.set_expanded(False)
        trainer_group.add(trainer_expander)

        # Trainer running selector
        self.trainer_running_row = Adw.ActionRow()
        self.trainer_running_row.set_title("Trainer (Running)")
        self.trainer_running_spinner = Gtk.Spinner()
        self.trainer_running_combo = Gtk.ComboBoxText()
        self.trainer_running_combo.set_hexpand(True)
        self.trainer_running_row.add_prefix(self.trainer_running_spinner)
        self.trainer_running_row.add_suffix(self.trainer_running_combo)
        trainer_expander.add_row(self.trainer_running_row)

        trainer_running_scan_row = Adw.ActionRow()
        trainer_running_scan_btn = Gtk.Button(label="Scan Trainer")
        trainer_running_scan_btn.get_style_context().add_class("suggested-action")
        trainer_running_scan_btn.connect(
            "clicked",
            lambda _: threading.Thread(target=self._fill_devices_trainer, daemon=True).start(),
        )
        trainer_running_scan_row.add_suffix(trainer_running_scan_btn)
        trainer_expander.add_row(trainer_running_scan_row)

        # Trainer-running HRM (separate)
        self.trainer_running_hr_row = Adw.ActionRow()
        self.trainer_running_hr_row.set_title("Trainer HRM (Running)")
        self.trainer_running_hr_spinner = Gtk.Spinner()
        self.trainer_running_hr_combo = Gtk.ComboBoxText()
        self.trainer_running_hr_combo.set_hexpand(True)
        self.trainer_running_hr_row.add_prefix(self.trainer_running_hr_spinner)
        self.trainer_running_hr_row.add_suffix(self.trainer_running_hr_combo)
        trainer_expander.add_row(self.trainer_running_hr_row)

        trainer_running_hr_scan_row = Adw.ActionRow()
        trainer_running_hr_scan_btn = Gtk.Button(label="Scan HRM")
        trainer_running_hr_scan_btn.get_style_context().add_class("suggested-action")
        trainer_running_hr_scan_btn.connect(
            "clicked",
            lambda _: threading.Thread(target=self._fill_devices_trainer_hr, daemon=True).start(),
        )
        trainer_running_hr_scan_row.add_suffix(trainer_running_hr_scan_btn)
        trainer_expander.add_row(trainer_running_hr_scan_row)

        # Trainer cycling selector
        self.trainer_cycling_row = Adw.ActionRow()
        self.trainer_cycling_row.set_title("Trainer (Cycling)")
        self.trainer_cycling_spinner = Gtk.Spinner()
        self.trainer_cycling_combo = Gtk.ComboBoxText()
        self.trainer_cycling_combo.set_hexpand(True)
        self.trainer_cycling_row.add_prefix(self.trainer_cycling_spinner)
        self.trainer_cycling_row.add_suffix(self.trainer_cycling_combo)
        trainer_expander.add_row(self.trainer_cycling_row)

        trainer_cycling_scan_row = Adw.ActionRow()
        trainer_cycling_scan_btn = Gtk.Button(label="Scan Trainer")
        trainer_cycling_scan_btn.get_style_context().add_class("suggested-action")
        trainer_cycling_scan_btn.connect(
            "clicked",
            lambda _: threading.Thread(target=self._fill_devices_trainer, daemon=True).start(),
        )
        trainer_cycling_scan_row.add_suffix(trainer_cycling_scan_btn)
        trainer_expander.add_row(trainer_cycling_scan_row)

        # Trainer Cycling HRM (separate)
        self.trainer_cycling_hr_row = Adw.ActionRow()
        self.trainer_cycling_hr_row.set_title("Trainer HRM (Cycling)")
        self.trainer_cycling_hr_spinner = Gtk.Spinner()
        self.trainer_cycling_hr_combo = Gtk.ComboBoxText()
        self.trainer_cycling_hr_combo.set_hexpand(True)
        self.trainer_cycling_hr_row.add_prefix(self.trainer_cycling_hr_spinner)
        self.trainer_cycling_hr_row.add_suffix(self.trainer_cycling_hr_combo)
        trainer_expander.add_row(self.trainer_cycling_hr_row)

        trainer_cycling_hr_scan_row = Adw.ActionRow()
        trainer_cycling_hr_scan_btn = Gtk.Button(label="Scan HRM")
        trainer_cycling_hr_scan_btn.get_style_context().add_class("suggested-action")
        trainer_cycling_hr_scan_btn.connect(
            "clicked",
            lambda _: threading.Thread(target=self._fill_devices_trainer_hr, daemon=True).start(),
        )
        trainer_cycling_hr_scan_row.add_suffix(trainer_cycling_hr_scan_btn)
        trainer_expander.add_row(trainer_cycling_hr_scan_row)

        devices_group.add(trainer_group)

        # ----- Pebble group -----
        pebble_group = Adw.PreferencesGroup()
        pebble_group.set_title("")

        # Enable
        pebble_enable_row = Adw.SwitchRow()
        pebble_enable_row.set_title("Enable Pebble")
        pebble_enable_row.set_active(self.app.app_settings.pebble.enable)
        pebble_group.add(pebble_enable_row)
        self.pebble_enable_row = pebble_enable_row

        pebble_expander = Adw.ExpanderRow()
        pebble_expander.set_title("Pebble Settings")
        pebble_expander.set_expanded(False)
        pebble_group.add(pebble_expander)

        pebble_emu_switch = Adw.SwitchRow()
        pebble_emu_switch.set_title("Use Emulator")
        pebble_emu_switch.set_active(self.app.app_settings.pebble.use_emulator)
        pebble_expander.add_row(pebble_emu_switch)
        self.pebble_emu_switch = pebble_emu_switch

        pebble_row = Adw.ActionRow()
        pebble_row.set_title("Pebble")
        pebble_spinner = Gtk.Spinner()
        pebble_combo = Gtk.ComboBoxText()
        pebble_combo.set_hexpand(False)
        pebble_combo.set_size_request(240, -1)
        pebble_combo.set_halign(Gtk.Align.END)
        pebble_combo.connect("changed", self._on_pebble_combo_changed)
        pebble_row.add_prefix(pebble_spinner)
        pebble_row.add_suffix(pebble_combo)
        if hasattr(pebble_row, "set_title_lines"):
            pebble_row.set_title_lines(1)
        pebble_expander.add_row(pebble_row)
        self.pebble_row = pebble_row
        self.pebble_spinner = pebble_spinner
        self.pebble_combo = pebble_combo

        # Emulator port (only visible when using emulator)
        pebble_port_row = Adw.ActionRow()
        pebble_port_row.set_title("Emulator Port")
        pebble_port_spin = Gtk.SpinButton.new_with_range(1, 65535, 1)
        pebble_port_spin.set_value(self.app.app_settings.pebble.port or 47527)
        pebble_port_spin.set_hexpand(False)
        pebble_port_spin.set_width_chars(6)
        pebble_port_row.add_suffix(pebble_port_spin)
        pebble_expander.add_row(pebble_port_row)
        self.pebble_port_row = pebble_port_row
        self.pebble_port_spin = pebble_port_spin

        # Scan button
        pebble_scan_row = Adw.ActionRow()
        pebble_scan_button = Gtk.Button(label="Scan Pebble")
        pebble_scan_button.get_style_context().add_class("suggested-action")
        pebble_scan_button.connect(
            "clicked",
            lambda _b: threading.Thread(target=self._fill_devices_pebble, daemon=True).start(),
        )
        pebble_scan_row.add_suffix(pebble_scan_button)
        pebble_expander.add_row(pebble_scan_row)
        self.pebble_scan_row = pebble_scan_row
        self.pebble_scan_button = pebble_scan_button

        def _update_pebble_expander_state(*_args):
            enabled = bool(self.pebble_enable_row.get_active()) if self.pebble_enable_row else False
            pebble_expander.set_sensitive(enabled)
            if not enabled:
                pebble_expander.set_expanded(False)
            pebble_expander.set_subtitle("Enabled" if enabled else "Disabled")

        pebble_enable_row.connect("notify::active", _update_pebble_expander_state)
        _update_pebble_expander_state()
        devices_group.add(pebble_group)

        # --- Providers group ---
        providers_group = Adw.PreferencesGroup()
        providers_group.set_title("Data Providers")

        # --- Intervals.icu provider ---
        icu_group = Adw.PreferencesGroup()
        icu_group.set_title("")

        icu_expander = Adw.ExpanderRow()
        icu_expander.set_title("Intervals.icu")
        icu_expander.set_expanded(False)
        icu_group.add(icu_expander)

        row_icu_id = Adw.ActionRow()
        row_icu_id.set_title("Athlete ID")
        self.icu_id_entry = Gtk.Entry()
        self.icu_id_entry.set_hexpand(True)
        self.icu_id_entry.set_text(self.app.app_settings.icu.athlete_id or "")
        row_icu_id.add_suffix(self.icu_id_entry)
        icu_expander.add_row(row_icu_id)

        row_icu_key = Adw.ActionRow()
        row_icu_key.set_title("API Key")
        self.icu_key_entry = Gtk.Entry()
        self.icu_key_entry.set_visibility(False)
        self.icu_key_entry.set_hexpand(True)
        self.icu_key_entry.set_text(self.app.app_settings.icu.api_key or "")
        row_icu_key.add_suffix(self.icu_key_entry)
        icu_expander.add_row(row_icu_key)

        def _update_icu_subtitle(*_args):
            aid = (self.icu_id_entry.get_text() or "").strip() if self.icu_id_entry else ""
            key = (self.icu_key_entry.get_text() or "").strip() if self.icu_key_entry else ""
            icu_expander.set_subtitle("Configured" if (aid and key) else "Not configured")

        self.icu_id_entry.connect("changed", _update_icu_subtitle)
        self.icu_key_entry.connect("changed", _update_icu_subtitle)
        _update_icu_subtitle()

        providers_group.add(icu_group)

        # ----- Database -----
        database_group = Adw.PreferencesGroup()
        database_group.set_title("")

        database_expander = Adw.ExpanderRow()
        database_expander.set_title("Database")
        database_expander.set_expanded(False)
        database_group.add(database_expander)

        dsn_row = Adw.ActionRow()
        dsn_row.set_title("Database DSN")
        self.dsn_entry = Gtk.Entry()
        self.dsn_entry.set_hexpand(True)
        self.dsn_entry.set_text(self.app.app_settings.database.dsn or "")
        dsn_row.add_suffix(self.dsn_entry)
        database_expander.add_row(dsn_row)

        def _update_db_subtitle(*_args):
            dsn = (self.dsn_entry.get_text() or "").strip() if self.dsn_entry else ""
            database_expander.set_subtitle("Configured" if dsn else "Not configured")

        self.dsn_entry.connect("changed", _update_db_subtitle)
        _update_db_subtitle()

        providers_group.add(database_group)

        # ----- Actions group -----
        action_group = Adw.PreferencesGroup()
        action_group.set_title("Actions")

        save_row = Adw.ActionRow()
        save_row.set_title("Save Settings")
        save_row.set_activatable(True)
        self.save_button = Gtk.Button(label="Save")
        self.save_button.get_style_context().add_class("suggested-action")
        self.save_button.connect("clicked", self._on_save_settings)
        save_row.add_suffix(self.save_button)
        action_group.add(save_row)

        self.row_fetch = Adw.ActionRow()
        self.row_fetch.set_title("Fetch Intervals.icu week")
        self.row_fetch.set_activatable(bool(self.app.app_settings.icu.api_key))
        self.btn_fetch_icu = Gtk.Button(label="Fetch")
        self.btn_fetch_icu.get_style_context().add_class("suggested-action")
        self.btn_fetch_icu.connect("clicked", self._on_fetch_icu)
        self.row_fetch.add_suffix(self.btn_fetch_icu)
        action_group.add(self.row_fetch)

        self.row_upload = Adw.ActionRow()
        self.row_upload.set_title("Upload to Intervals.icu")
        self.row_upload.set_activatable(bool(self.app.app_settings.icu.api_key))
        self.btn_upload_icu = Gtk.Button(label="Upload")
        self.btn_upload_icu.get_style_context().add_class("suggested-action")
        self.btn_upload_icu.connect("clicked", self._on_upload_icu)
        self.row_upload.add_suffix(self.btn_upload_icu)
        action_group.add(self.row_upload)

        self.row_sync = Adw.ActionRow()
        self.row_sync.set_title("Sync to Database")
        self.row_sync.set_activatable(bool(self.app.app_settings.database.dsn))
        self.sync_button = Gtk.Button(label="Sync")
        self.sync_button.get_style_context().add_class("suggested-action")
        self.sync_button.connect("clicked", self._on_sync)
        self.row_sync.add_suffix(self.sync_button)
        action_group.add(self.row_sync)

        # Layout container
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        container.set_margin_top(12)
        container.set_margin_bottom(12)
        container.set_margin_start(12)
        container.set_margin_end(12)

        container.append(personal_group)
        container.append(devices_group)
        container.append(providers_group)
        container.append(action_group)

        # Return the scroller so the page scrolls on small windows
        scroller.set_child(container)

        # Prepopulate HRM
        self._combo_set_items_with_none(
            self.hr_combo,
            (
                [self.app.app_settings.running_sensors.hr_name]
                if self.app.app_settings.running_sensors.hr_name
                else []
            ),
            self.app.app_settings.running_sensors.hr_name,
        )
        self._combo_set_items_with_none(
            self.cycling_hr_combo,
            (
                [self.app.app_settings.cycling_sensors.hr_name]
                if self.app.app_settings.cycling_sensors.hr_name
                else []
            ),
            self.app.app_settings.cycling_sensors.hr_name,
        )
        self.hr_map = (
            {
                self.app.app_settings.running_sensors.hr_name: self.app.app_settings.running_sensors.hr_address
            }
            if self.app.app_settings.running_sensors.hr_name
            else {}
        )

        # Prepopulate Speed
        self._combo_set_items_with_none(
            self.speed_combo,
            (
                [self.app.app_settings.running_sensors.speed_name]
                if self.app.app_settings.running_sensors.speed_name
                else []
            ),
            self.app.app_settings.running_sensors.speed_name,
        )
        self._combo_set_items_with_none(
            self.cycling_speed_combo,
            (
                [self.app.app_settings.cycling_sensors.speed_name]
                if self.app.app_settings.cycling_sensors.speed_name
                else []
            ),
            self.app.app_settings.cycling_sensors.speed_name,
        )
        self.speed_map = (
            {
                self.app.app_settings.running_sensors.speed_name: self.app.app_settings.running_sensors.speed_address
            }
            if self.app.app_settings.running_sensors.speed_name
            else {}
        )

        # Prepopulate Cadence
        self._combo_set_items_with_none(
            self.cadence_combo,
            (
                [self.app.app_settings.running_sensors.cadence_name]
                if self.app.app_settings.running_sensors.cadence_name
                else []
            ),
            self.app.app_settings.running_sensors.cadence_name,
        )
        self._combo_set_items_with_none(
            self.cycling_cadence_combo,
            (
                [self.app.app_settings.cycling_sensors.cadence_name]
                if self.app.app_settings.cycling_sensors.cadence_name
                else []
            ),
            self.app.app_settings.cycling_sensors.cadence_name,
        )
        self.cadence_map = (
            {
                self.app.app_settings.running_sensors.cadence_name: self.app.app_settings.running_sensors.cadence_address
            }
            if self.app.app_settings.running_sensors.cadence_name
            else {}
        )

        # Prepopulate Power
        self._combo_set_items_with_none(
            self.power_combo,
            (
                [self.app.app_settings.running_sensors.power_name]
                if self.app.app_settings.running_sensors.power_name
                else []
            ),
            self.app.app_settings.running_sensors.power_name,
        )
        self._combo_set_items_with_none(
            self.cycling_power_combo,
            (
                [self.app.app_settings.cycling_sensors.power_name]
                if self.app.app_settings.cycling_sensors.power_name
                else []
            ),
            self.app.app_settings.cycling_sensors.power_name,
        )
        self.power_map = (
            {
                self.app.app_settings.running_sensors.power_name: self.app.app_settings.running_sensors.power_address
            }
            if self.app.app_settings.running_sensors.power_name
            else {}
        )

        # Prepopulate Trainers plus their HRMs
        self._combo_set_items_with_none(
            self.trainer_running_combo,
            [self.app.app_settings.trainer_running.trainer_name]
            if self.app.app_settings.trainer_running.trainer_name
            else [],
            self.app.app_settings.trainer_running.trainer_name,
        )
        self._combo_set_items_with_none(
            self.trainer_cycling_combo,
            [self.app.app_settings.trainer_cycling.trainer_name]
            if self.app.app_settings.trainer_cycling.trainer_name
            else [],
            self.app.app_settings.trainer_cycling.trainer_name,
        )
        self.trainer_running_map = (
            {
                self.app.app_settings.trainer_running.trainer_name: {
                    "address": self.app.app_settings.trainer_running.trainer_address,
                    "machine_type": self.app.app_settings.trainer_running.trainer_machine_type,
                },
            }
            if self.app.app_settings.trainer_running.trainer_name
            else {}
        )
        self.trainer_cycling_map = (
            {
                self.app.app_settings.trainer_cycling.trainer_name: {
                    "address": self.app.app_settings.trainer_cycling.trainer_address,
                    "machine_type": self.app.app_settings.trainer_cycling.trainer_machine_type,
                },
            }
            if self.app.app_settings.trainer_cycling.trainer_name
            else {}
        )

        self._combo_set_items_with_none(
            self.trainer_running_hr_combo,
            [self.app.app_settings.trainer_running.hr_name]
            if self.app.app_settings.trainer_running.hr_name
            else [],
            self.app.app_settings.trainer_running.hr_name,
        )
        self._combo_set_items_with_none(
            self.trainer_cycling_hr_combo,
            [self.app.app_settings.trainer_cycling.hr_name]
            if self.app.app_settings.trainer_cycling.hr_name
            else [],
            self.app.app_settings.trainer_cycling.hr_name,
        )
        self.trainer_running_hr_map = (
            {
                self.app.app_settings.trainer_running.hr_name: self.app.app_settings.trainer_running.hr_address
            }
            if self.app.app_settings.trainer_running.hr_name
            else {}
        )
        self.trainer_cycling_hr_map = (
            {
                self.app.app_settings.trainer_cycling.hr_name: self.app.app_settings.trainer_cycling.hr_address
            }
            if self.app.app_settings.trainer_cycling.hr_name
            else {}
        )

        # Prepopulate Pebble
        if self.app.app_settings.pebble.use_emulator and self.pebble_row:
            self.pebble_row.set_subtitle("Emulator mode")
        if self.app.app_settings.pebble.name and self.pebble_combo:
            self.pebble_combo.append_text(self.app.app_settings.pebble.name)
            self.pebble_combo.set_active(0)
            self.pebble_map = {
                self.app.app_settings.pebble.name: self.app.app_settings.pebble.address,
            }

        if self.pebble_emu_switch:
            self.pebble_emu_switch.connect("notify::active", self._on_pebble_mode_toggled)
            self._on_pebble_mode_toggled(self.pebble_emu_switch)

        self._update_actions_state()

        return scroller

    def _set_action_enabled(self, row: Adw.ActionRow, button: Gtk.Button, enabled: bool):
        # Disable the actual clickable widget
        button.set_sensitive(enabled)

        # Optional: also grey out the whole row (subtitle, label, etc.)
        row.set_sensitive(enabled)

        # Optional: suggested-action class makes it look "primary" even when disabled
        ctx = button.get_style_context()
        if enabled:
            ctx.add_class("suggested-action")
        else:
            ctx.remove_class("suggested-action")

    def _update_actions_state(self, *_args):
        intervals_athlete_id = (
            (self.icu_id_entry.get_text() or "").strip() if self.icu_id_entry else None
        )
        intervals_key = (
            (self.icu_key_entry.get_text() or "").strip() if self.icu_key_entry else None
        )
        database_dsn = (self.dsn_entry.get_text() or "").strip() if self.dsn_entry else None

        icu_ok = bool(intervals_athlete_id and intervals_key)
        db_ok = bool(database_dsn)

        self._set_action_enabled(self.row_fetch, self.btn_fetch_icu, icu_ok)
        self._set_action_enabled(self.row_upload, self.btn_upload_icu, icu_ok)
        self._set_action_enabled(self.row_sync, self.sync_button, db_ok)

    # ----- Scanners -----
    def _fill_devices_hr(self):
        GLib.idle_add(self.hr_spinner.start)
        self.hr_row.set_subtitle("Scanning for HRM…")

        GLib.idle_add(self.cycling_hr_spinner.start)
        self.cycling_hr_row.set_subtitle("Scanning for HRM…")

        async def _scan():
            devices = await discover_heart_rate_devices(scan_timeout=5.0)
            mapping = {d.name: d.address for d in devices if d.name}
            names = sorted(mapping.keys())

            def _apply():
                self.hr_spinner.stop()
                self.hr_row.set_subtitle("" if names else "No HRM found")

                self.cycling_hr_spinner.stop()
                self.cycling_hr_row.set_subtitle("" if names else "No HRM found")

                self._combo_set_items_with_none(
                    self.hr_combo,
                    names,
                    self.app.app_settings.running_sensors.hr_name,
                )
                self._combo_set_items_with_none(
                    self.cycling_hr_combo,
                    names,
                    self.app.app_settings.cycling_sensors.hr_name,
                )
                self.hr_map = mapping

            GLib.idle_add(_apply)

        asyncio.run(_scan())

    def _fill_devices_speed_cadence(self):
        GLib.idle_add(self.speed_spinner.start)
        self.speed_row.set_subtitle("Scanning for speed/cadence devices…")

        GLib.idle_add(self.cadence_spinner.start)
        self.cadence_row.set_subtitle("Scanning for speed/cadence devices…")

        GLib.idle_add(self.cycling_speed_spinner.start)
        self.cycling_speed_row.set_subtitle("Scanning for speed/cadence devices…")

        GLib.idle_add(self.cycling_cadence_spinner.start)
        self.cycling_cadence_row.set_subtitle("Scanning for speed/cadence devices…")

        async def _scan():
            devices = await discover_speed_cadence_devices(scan_timeout=5.0)
            mapping = {d.name: d.address for d in devices if d.name}
            names = sorted(mapping.keys())

            def _apply():
                self.speed_spinner.stop()
                self.speed_row.set_subtitle("" if names else "No speed devices found")

                self.cycling_speed_spinner.stop()
                self.cycling_speed_row.set_subtitle("" if names else "No speed devices found")

                self._combo_set_items_with_none(
                    self.speed_combo,
                    names,
                    self.app.app_settings.running_sensors.speed_name,
                )
                self._combo_set_items_with_none(
                    self.cycling_speed_combo,
                    names,
                    self.app.app_settings.cycling_sensors.speed_name,
                )
                self.speed_map = mapping

                # Cadence
                self.cadence_spinner.stop()
                self.cadence_row.set_subtitle("" if names else "No cadence devices found")

                self.cycling_cadence_spinner.stop()
                self.cycling_cadence_row.set_subtitle("" if names else "No cadence devices found")

                self._combo_set_items_with_none(
                    self.cadence_combo,
                    names,
                    self.app.app_settings.running_sensors.cadence_name,
                )
                self._combo_set_items_with_none(
                    self.cycling_cadence_combo,
                    names,
                    self.app.app_settings.cycling_sensors.cadence_name,
                )
                self.cadence_map = mapping

            GLib.idle_add(_apply)

        asyncio.run(_scan())

    def _fill_devices_power(self):
        GLib.idle_add(self.power_spinner.start)
        self.power_row.set_subtitle("Scanning for power devices…")

        GLib.idle_add(self.cycling_power_spinner.start)
        self.cycling_power_row.set_subtitle("Scanning for power devices…")

        async def _scan():
            devices = await discover_power_devices(scan_timeout=5.0)
            mapping = {d.name: d.address for d in devices if d.name}
            names = sorted(mapping.keys())

            def _apply():
                self.power_spinner.stop()
                self.power_row.set_subtitle("" if names else "No power devices found")

                self.cycling_power_spinner.stop()
                self.cycling_power_row.set_subtitle("" if names else "No power devices found")

                self._combo_set_items_with_none(
                    self.power_combo,
                    names,
                    self.app.app_settings.running_sensors.power_name,
                )
                self._combo_set_items_with_none(
                    self.cycling_power_combo,
                    names,
                    self.app.app_settings.cycling_sensors.power_name,
                )
                self.power_map = mapping

            GLib.idle_add(_apply)

        asyncio.run(_scan())

    def _fill_devices_trainer_hr(self):
        GLib.idle_add(self.trainer_cycling_hr_spinner.start)
        GLib.idle_add(self.trainer_cycling_hr_row.set_subtitle, "Scanning for HRM…")
        GLib.idle_add(self.trainer_running_hr_spinner.start)
        GLib.idle_add(self.trainer_running_hr_row.set_subtitle, "Scanning for HRM…")

        async def _scan():
            devices = await discover_heart_rate_devices(scan_timeout=5.0)
            mapping = {d.name: d.address for d in devices if d.name}
            names = sorted(mapping.keys())

            def _apply():
                # Cycling HRM
                self.trainer_cycling_hr_spinner.stop()
                self.trainer_cycling_hr_row.set_subtitle("" if names else "No HRM found")
                self._combo_set_items_with_none(
                    self.trainer_cycling_hr_combo,
                    names,
                    self.app.app_settings.trainer_cycling.hr_name,
                )
                self.trainer_cycling_hr_map = mapping

                # Running HRM
                self.trainer_running_hr_spinner.stop()
                self.trainer_running_hr_row.set_subtitle("" if names else "No HRM found")
                self._combo_set_items_with_none(
                    self.trainer_running_hr_combo,
                    names,
                    self.app.app_settings.trainer_running.hr_name,
                )
                self.trainer_running_hr_map = mapping

            GLib.idle_add(_apply)

        asyncio.run(_scan())

    def _fill_devices_trainer(self):
        GLib.idle_add(self.trainer_running_spinner.start)
        GLib.idle_add(self.trainer_cycling_spinner.start)
        GLib.idle_add(self.trainer_running_row.set_subtitle, "Scanning for FTMS trainers…")
        GLib.idle_add(self.trainer_cycling_row.set_subtitle, "Scanning for FTMS trainers…")

        async def _scan():
            found = await discover_ftms_devices(scan_timeout=5.0)
            logger.debug(f"Found FTMS devices: {found}")

            mapping = {}
            for dev, mtype in found:
                name = getattr(dev, "name", None) or "(unnamed)"
                addr = getattr(dev, "address", None) or ""
                mt = mtype if mtype is not None else ""
                disp = f"{name} [{addr}]"
                logger.debug(f"Mapping trainer: {disp} -> {addr} ({mt})")
                mapping[disp] = {"address": addr, "machine_type": mt}

            names = sorted(mapping.keys())

            def _apply():
                self.trainer_running_spinner.stop()
                self.trainer_running_row.set_subtitle("" if names else "No FTMS trainers found")
                self.trainer_cycling_spinner.stop()
                self.trainer_cycling_row.set_subtitle("" if names else "No FTMS trainers found")

                self._combo_set_items_with_none(
                    self.trainer_running_combo,
                    names,
                    self.app.app_settings.trainer_running.trainer_name,
                )
                self._combo_set_items_with_none(
                    self.trainer_cycling_combo,
                    names,
                    self.app.app_settings.trainer_cycling.trainer_name,
                )
                self.trainer_running_map = mapping
                self.trainer_cycling_map = mapping

            GLib.idle_add(_apply)

        asyncio.run(_scan())

    def _fill_devices_pebble(self):
        if not self.pebble_spinner or not self.pebble_row or not self.pebble_combo:
            return

        GLib.idle_add(self.pebble_spinner.start)
        GLib.idle_add(self.pebble_row.set_subtitle, "Scanning for Pebble…")

        def _scan_cli() -> dict[str, str]:
            """
            Use 'bluetoothctl devices' to list
            known/paired BT Classic devices, filter those with 'Pebble' in the name.
            Returns {name: mac}.
            """
            mapping: dict[str, str] = {}
            outputs = []
            with contextlib.suppress(Exception):
                outputs.append(
                    subprocess.check_output(["bluetoothctl", "devices"], text=True),
                )

            for out in outputs:
                for line in out.splitlines():
                    # Format: "Device AA:BB:CC:DD:EE:FF Some Name"
                    parts = line.strip().split(" ", 2)
                    if len(parts) >= 3 and parts[0] in ("Device", "dev"):
                        mac = parts[1].strip()
                        name = parts[2].strip()
                        if "pebble" in name.lower():
                            mapping[name] = mac
            return mapping

        def _uniq_display_names(name_to_mac: dict[str, str]) -> dict[str, str]:
            """
            Make human-friendly, MAC-free display names; if duplicates exist,
            suffix with (2), (3), … to disambiguate.
            """
            counts: dict[str, int] = {}
            for n in name_to_mac:
                counts[n] = counts.get(n, 0) + 1
            seen_idx: dict[str, int] = {}
            display_to_mac: dict[str, str] = {}
            for name, mac in name_to_mac.items():
                if counts[name] == 1:
                    disp = name
                else:
                    i = seen_idx.get(name, 0) + 1
                    seen_idx[name] = i
                    disp = f"{name} ({i})"
                display_to_mac[disp] = mac
            return display_to_mac

        def worker():
            if not self.pebble_spinner or not self.pebble_row or not self.pebble_combo:
                return

            try:
                name_to_mac = _scan_cli()
            except Exception as e:
                name_to_mac = {}
                GLib.idle_add(self.pebble_row.set_subtitle, f"Scan failed: {e}")

            display_map = _uniq_display_names(name_to_mac)
            names = sorted(display_map.keys())

            def _update_ui():
                if not self.pebble_spinner or not self.pebble_row or not self.pebble_combo:
                    return False

                self.pebble_spinner.stop()
                self.pebble_combo.remove_all()
                for disp in names:
                    self.pebble_combo.append_text(disp)
                if not names:
                    self.pebble_row.set_subtitle("No Pebble devices found")
                else:
                    self.pebble_row.set_subtitle("")
                    # auto-select saved MAC if present
                    if self.app.app_settings.pebble.address:
                        for i, disp in enumerate(names):
                            if display_map[disp] == self.app.app_settings.pebble.address:
                                self.pebble_combo.set_active(i)
                                break
                self.pebble_map = display_map
                return False

            GLib.idle_add(_update_ui)

        threading.Thread(target=worker, daemon=True).start()

    def _on_pebble_combo_changed(self, _combo: Gtk.ComboBoxText):
        """Show the MAC in a tooltip only (keeps UI clean)."""
        if not self.pebble_combo or not self.pebble_map:
            return
        disp = self.pebble_combo.get_active_text() or ""
        mac = self.pebble_map.get(disp, "")
        self.pebble_combo.set_tooltip_text(mac or None)

    def _on_pebble_mode_toggled(self, switch, _pspec=None):
        """Hide BT selection rows when using the emulator to save space."""
        use_emu = switch.get_active()
        if self.pebble_row:
            self.pebble_row.set_visible(not use_emu)
            self.pebble_row.set_subtitle("Emulator mode" if use_emu else "")
        if self.pebble_scan_row:
            self.pebble_scan_row.set_visible(not use_emu)
        if self.pebble_port_row:
            self.pebble_port_row.set_visible(use_emu)
        return False

    def _on_fetch_icu(self, _button: Gtk.Button):
        if not self.icu_id_entry or not self.icu_key_entry or not self.btn_fetch_icu:
            return

        out_dir_running = self.app.workouts_running_dir / "intervals_icu"
        out_dir_running.mkdir(parents=True, exist_ok=True)

        out_dir_cycling = self.app.workouts_cycling_dir / "intervals_icu"
        out_dir_cycling.mkdir(parents=True, exist_ok=True)

        aid = (self.app.app_settings.icu.athlete_id or "").strip()
        key = (self.app.app_settings.icu.api_key or "").strip()
        if not (aid and key):
            self.app.show_toast("Intervals.icu Athlete ID and API key required")
            return

        self.btn_fetch_icu.set_sensitive(False)

        def worker():
            try:
                provider = workout_providers.IntervalsICUProvider(
                    athlete_id=aid,
                    api_key=key,
                    ext="fit",
                )

                # Beginning of the day
                start = datetime.today().replace(hour=0, minute=0, second=0, microsecond=0)
                end = start + timedelta(days=6)
                end = end.replace(hour=23, minute=59, second=59)

                provider.fetch_between(SportTypesEnum.running, start, end, out_dir_running)
                provider.fetch_between(SportTypesEnum.biking, start, end, out_dir_cycling)

                # simply refresh the existing list
                GLib.idle_add(self.app.tracker.mode_view.refresh)
            except requests.HTTPError as e:
                GLib.idle_add(self.app.show_toast, f"Intervals.icu error: {e.response.status_code}")
            except Exception as e:
                GLib.idle_add(self.app.show_toast, f"Fetch failed: {e}")
            finally:
                if self.btn_fetch_icu:
                    GLib.idle_add(self.btn_fetch_icu.set_sensitive, True)

        threading.Thread(target=worker, daemon=True).start()

    def _on_save_settings(self, _button):
        self.app.app_settings.database.dsn = self.dsn_entry.get_text()

        # Running sensors
        # HR
        selected = self.hr_combo.get_active_text()
        if selected == NONE_LABEL or not selected:
            self.app.app_settings.running_sensors.hr_name = None
            self.app.app_settings.running_sensors.hr_address = None
        else:
            self.app.app_settings.running_sensors.hr_name = selected
            self.app.app_settings.running_sensors.hr_address = self.hr_map.get(selected)

        # Speed
        selected = self.speed_combo.get_active_text()
        if selected == NONE_LABEL or not selected:
            self.app.app_settings.running_sensors.speed_name = None
            self.app.app_settings.running_sensors.speed_address = None
        else:
            self.app.app_settings.running_sensors.speed_name = selected
            self.app.app_settings.running_sensors.speed_address = self.speed_map.get(selected)
        # Cadence
        selected = self.cadence_combo.get_active_text()
        if selected == NONE_LABEL or not selected:
            self.app.app_settings.running_sensors.cadence_name = None
            self.app.app_settings.running_sensors.cadence_address = None
        else:
            self.app.app_settings.running_sensors.cadence_name = selected
            self.app.app_settings.running_sensors.cadence_address = self.cadence_map.get(selected)

        # Power
        selected = self.power_combo.get_active_text()
        if selected == NONE_LABEL or not selected:
            self.app.app_settings.running_sensors.power_name = None
            self.app.app_settings.running_sensors.power_address = None
        else:
            self.app.app_settings.running_sensors.power_name = selected
            self.app.app_settings.running_sensors.power_address = self.power_map.get(selected)
        # Cycling sensors
        # HR
        selected = self.cycling_hr_combo.get_active_text()
        if selected == NONE_LABEL or not selected:
            self.app.app_settings.cycling_sensors.hr_name = None
            self.app.app_settings.cycling_sensors.hr_address = None
        else:
            self.app.app_settings.cycling_sensors.hr_name = selected
            self.app.app_settings.cycling_sensors.hr_address = self.hr_map.get(selected)

        # Speed
        selected = self.cycling_speed_combo.get_active_text()
        if selected == NONE_LABEL or not selected:
            self.app.app_settings.cycling_sensors.speed_name = None
            self.app.app_settings.cycling_sensors.speed_address = None
        else:
            self.app.app_settings.cycling_sensors.speed_name = selected
            self.app.app_settings.cycling_sensors.speed_address = self.speed_map.get(selected)
        # Cadence
        selected = self.cycling_cadence_combo.get_active_text()
        if selected == NONE_LABEL or not selected:
            self.app.app_settings.cycling_sensors.cadence_name = None
            self.app.app_settings.cycling_sensors.cadence_address = None
        else:
            self.app.app_settings.cycling_sensors.cadence_name = selected
            self.app.app_settings.cycling_sensors.cadence_address = self.cadence_map.get(selected)

        # Power
        selected = self.cycling_power_combo.get_active_text()
        if selected == NONE_LABEL or not selected:
            self.app.app_settings.cycling_sensors.power_name = None
            self.app.app_settings.cycling_sensors.power_address = None
        else:
            self.app.app_settings.cycling_sensors.power_name = selected
            self.app.app_settings.cycling_sensors.power_address = self.power_map.get(selected)
        # Pebble
        self.app.app_settings.pebble.enable = (
            self.pebble_enable_row.get_active() if self.pebble_enable_row else False
        )
        self.app.app_settings.pebble.use_emulator = (
            self.pebble_emu_switch.get_active() if self.pebble_emu_switch else False
        )
        if self.pebble_port_spin:
            self.app.app_settings.pebble.port = self.pebble_port_spin.get_value_as_int()
        if self.app.app_settings.pebble.use_emulator:
            self.app.app_settings.pebble.name = None
            self.app.app_settings.pebble.address = None
        else:
            disp = self.pebble_combo.get_active_text() if self.pebble_combo else ""
            self.app.app_settings.pebble.name = disp
            self.app.app_settings.pebble.address = self.pebble_map.get(disp)

        self.app.app_settings.personal.weight_kg = self.weight_spin.get_value_as_int()
        self.app.app_settings.personal.resting_hr = self.rest_spin.get_value_as_int()
        self.app.app_settings.personal.max_hr = self.max_spin.get_value_as_int()
        self.app.app_settings.personal.ftp_watts = self.ftp_spin.get_value_as_int()

        self.app.app_settings.icu.athlete_id = (
            self.icu_id_entry.get_text().strip() if self.icu_id_entry else None
        )
        self.app.app_settings.icu.api_key = (
            self.icu_key_entry.get_text().strip() if self.icu_key_entry else None
        )

        # Trainer running
        selected = self.trainer_running_combo.get_active_text()
        if selected == NONE_LABEL or not selected:
            self.app.app_settings.trainer_running.trainer_name = None
            self.app.app_settings.trainer_running.trainer_address = None
            self.app.app_settings.trainer_running.trainer_machine_type = None
        else:
            self.app.app_settings.trainer_running.trainer_name = selected
            trainer_info = self.trainer_running_map.get(selected, {})
            self.app.app_settings.trainer_running.trainer_address = trainer_info.get("address")
            self.app.app_settings.trainer_running.trainer_machine_type = trainer_info.get(
                "machine_type",
            )

        # Trainer Running HRM
        if self.trainer_running_hr_combo:
            sel = self.trainer_running_hr_combo.get_active_text()
            if sel == NONE_LABEL or not sel:
                self.app.app_settings.trainer_running.hr_name = None
                self.app.app_settings.trainer_running.hr_address = None
            else:
                self.app.app_settings.trainer_running.hr_name = sel
                self.app.app_settings.trainer_running.hr_address = self.trainer_running_hr_map.get(
                    sel,
                )

        # Trainer cycling
        if self.trainer_cycling_combo:
            selected = self.trainer_cycling_combo.get_active_text()
            if selected == NONE_LABEL or not selected:
                self.app.app_settings.trainer_cycling.trainer_name = None
                self.app.app_settings.trainer_cycling.trainer_address = None
                self.app.app_settings.trainer_cycling.trainer_machine_type = None
            else:
                self.app.app_settings.trainer_cycling.trainer_name = selected
                trainer_info = self.trainer_cycling_map.get(selected, {})
                self.app.app_settings.trainer_cycling.trainer_address = trainer_info.get(
                    "address",
                )
                self.app.app_settings.trainer_cycling.trainer_machine_type = trainer_info.get(
                    "machine_type",
                )

        # Trainer Cycling HRM
        if self.trainer_cycling_hr_combo:
            sel = self.trainer_cycling_hr_combo.get_active_text()
            if sel == NONE_LABEL or not sel:
                self.app.app_settings.trainer_cycling.hr_name = None
                self.app.app_settings.trainer_cycling.hr_address = None
            else:
                self.app.app_settings.trainer_cycling.hr_name = sel
                self.app.app_settings.trainer_cycling.hr_address = self.trainer_cycling_hr_map.get(
                    sel,
                )

        self.app.app_settings.save()
        self._update_actions_state()

        # Apply Pebble settings right away (start/stop bridge without restart)
        GLib.idle_add(self.app.apply_pebble_settings)

        # Apply sensor settings right away (start/stop recorder with new sensors without restart)
        GLib.idle_add(self.app.apply_sensor_settings)

        toast = Adw.Toast.new("Settings saved successfully")
        GLib.idle_add(self.app.toast_overlay.add_toast, toast)

        GLib.idle_add(self.app.tracker.redraw)
        GLib.idle_add(self.app.history.refresh)

    def _on_sync(self, button: Gtk.Button):
        # disable the Settings-page sync button
        button.set_sensitive(False)
        GLib.idle_add(self.app.show_toast, "Syncing…")

        def do_sync():
            if not self.app.app_settings.database.dsn:
                GLib.idle_add(self.app.show_toast, "No database DSN configured")
                GLib.idle_add(button.set_sensitive, True)
                return

            try:
                self.app.recorder.db.sync_to_database(self.app.app_settings.database.dsn)
            except ConnectionError as e:
                GLib.idle_add(self.app.show_toast, f"Sync failed: {e}")
                GLib.idle_add(button.set_sensitive, True)
                return

            # refresh history after a successful sync
            # GLib.idle_add(self._clear_history)
            # threading.Thread(target=self._load_history, daemon=True).start()

            GLib.idle_add(self.app.show_toast, "Sync complete")
            GLib.idle_add(button.set_sensitive, True)

        threading.Thread(target=do_sync, daemon=True).start()

    def _on_upload_icu(self, _button: Gtk.Button):
        if not self.app.app_settings.icu.api_key:
            self.app.show_toast("Intervals.icu API key required")
            return

        if self.btn_upload_icu:
            self.btn_upload_icu.set_sensitive(False)
        self.app.show_toast("Uploading…")

        def worker():
            try:
                provider = upload_providers.IntervalsICUUploader(
                    athlete_id=self.app.app_settings.icu.athlete_id,
                    api_key=self.app.app_settings.icu.api_key,
                )
                results = provider.upload_not_uploaded(self.app)
                if not results:
                    GLib.idle_add(self.app.show_toast, "No new activities to upload")
                    return

                ok = sum(1 for _, s, _ in results if s)
                fail = [err for _, s, err in results if not s]
                if ok:
                    GLib.idle_add(
                        self.app.show_toast,
                        f"✅ Uploaded {ok} new {'activities' if ok > 1 else 'activity'}",
                    )
                if fail:
                    GLib.idle_add(self.app.show_toast, f"⚠️ {len(fail)} failed")
            except Exception as e:
                GLib.idle_add(self.app.show_toast, f"Upload failed: {e}")
            finally:
                if self.btn_upload_icu:
                    GLib.idle_add(self.btn_upload_icu.set_sensitive, True)

        threading.Thread(target=worker, daemon=True).start()

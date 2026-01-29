import asyncio
import contextlib
import datetime
import subprocess
import threading
from configparser import ConfigParser

import gi
import requests
from bleak import BleakScanner
from bleaksport.discover import discover_power_devices, discover_speed_cadence_devices

from fitness_tracker import upload_providers, workout_providers
from fitness_tracker.hr_provider import HEART_RATE_SERVICE_UUID

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Adw, GLib, Gtk  # noqa: E402


class SettingsPageUI:
    def __init__(self, app: "FitnessAppUI"):
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

        # pebble
        self.pebble_map: dict[str, str] = {}

        # Intervals ICU
        self.icu_id_entry = None
        self.icu_key_entry = None
        self.btn_fetch_icu = None
        self.btn_upload_icu = None

    def build_page(self) -> Gtk.Widget:
        # Outer scroller so the page never overflows vertically
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)

        # ----- Personal settings group -----
        personal_group = Adw.PreferencesGroup()
        personal_group.set_title("Personal Info")

        # Resting HR
        rest_row = Adw.ActionRow()
        rest_row.set_title("Resting HR")
        self.rest_spin = Gtk.SpinButton.new_with_range(30, 120, 1)
        self.rest_spin.set_value(self.app.resting_hr)
        rest_row.add_suffix(self.rest_spin)
        personal_group.add(rest_row)

        # Max HR
        max_row = Adw.ActionRow()
        max_row.set_title("Max HR")
        self.max_spin = Gtk.SpinButton.new_with_range(100, 250, 1)
        self.max_spin.set_value(self.app.max_hr)
        max_row.add_suffix(self.max_spin)
        personal_group.add(max_row)

        # FTP (for workouts)
        ftp_row = Adw.ActionRow()
        ftp_row.set_title("FTP (Watts)")
        self.ftp_spin = Gtk.SpinButton.new_with_range(50, 2000, 1)
        self.ftp_spin.set_value(self.app.ftp_watts)
        ftp_row.add_suffix(self.ftp_spin)
        personal_group.add(ftp_row)

        # ----- Devices group -----
        devices_group = Adw.PreferencesGroup()
        devices_group.set_title("Devices")

        # ----- Sensors group -----
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

        # ----- Pebble group -----
        pebble_group = Adw.PreferencesGroup()
        pebble_group.set_title("")

        # Enable
        pebble_enable_row = Adw.SwitchRow()
        pebble_enable_row.set_title("Enable Pebble")
        pebble_enable_row.set_active(self.app.pebble_enable)
        pebble_group.add(pebble_enable_row)
        self.pebble_enable_row = pebble_enable_row

        pebble_expander = Adw.ExpanderRow()
        pebble_expander.set_title("Pebble Settings")
        pebble_expander.set_expanded(False)
        pebble_group.add(pebble_expander)

        pebble_emu_switch = Adw.SwitchRow()
        pebble_emu_switch.set_title("Use Emulator")
        pebble_emu_switch.set_active(self.app.pebble_use_emulator)
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
        pebble_port_spin.set_value(self.app.pebble_port or 47527)
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
        self.icu_id_entry.set_text(self.app.icu_athlete_id or "")
        row_icu_id.add_suffix(self.icu_id_entry)
        icu_expander.add_row(row_icu_id)

        row_icu_key = Adw.ActionRow()
        row_icu_key.set_title("API Key")
        self.icu_key_entry = Gtk.Entry()
        self.icu_key_entry.set_visibility(False)
        self.icu_key_entry.set_hexpand(True)
        self.icu_key_entry.set_text(self.app.icu_api_key or "")
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
        self.dsn_entry.set_text(self.app.database_dsn)
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

        row_fetch = Adw.ActionRow()
        row_fetch.set_title("Fetch Intervals.icu week")
        row_fetch.set_activatable(bool(self.app.icu_api_key))
        self.btn_fetch_icu = Gtk.Button(label="Fetch")
        self.btn_fetch_icu.get_style_context().add_class("suggested-action")
        self.btn_fetch_icu.connect("clicked", self._on_fetch_icu)
        row_fetch.add_suffix(self.btn_fetch_icu)
        action_group.add(row_fetch)

        row_upload = Adw.ActionRow()
        row_upload.set_title("Upload to Intervals.icu")
        row_upload.set_activatable(bool(self.app.icu_api_key))
        self.btn_upload_icu = Gtk.Button(label="Upload")
        self.btn_upload_icu.get_style_context().add_class("suggested-action")
        self.btn_upload_icu.connect("clicked", self._on_upload_icu)
        row_upload.add_suffix(self.btn_upload_icu)
        action_group.add(row_upload)

        sync_row = Adw.ActionRow()
        sync_row.set_title("Sync to Database")
        sync_row.set_activatable(bool(self.app.database_dsn))
        self.sync_button = Gtk.Button(label="Sync")
        self.sync_button.get_style_context().add_class("suggested-action")
        self.sync_button.connect("clicked", self._on_sync)
        sync_row.add_suffix(self.sync_button)
        action_group.add(sync_row)

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
        if self.app.hr_name:
            self.hr_spinner.stop()
            self.hr_combo.append_text(self.app.hr_name)
            self.hr_combo.set_active(0)
            self.hr_map = {self.app.hr_name: self.app.hr_address}

        # Prepopulate Speed
        if self.app.speed_name:
            self.speed_spinner.stop()
            self.speed_combo.append_text(self.app.speed_name)
            self.speed_combo.set_active(0)
            self.speed_map = {self.app.speed_name: self.app.speed_address}

        # Prepopulate Cadence
        if self.app.cadence_name:
            self.cadence_spinner.stop()
            self.cadence_combo.append_text(self.app.cadence_name)
            self.cadence_combo.set_active(0)
            self.cadence_map = {self.app.cadence_name: self.app.cadence_address}

        # Prepopulate Power
        if self.app.power_name:
            self.power_spinner.stop()
            self.power_combo.append_text(self.app.power_name)
            self.power_combo.set_active(0)
            self.power_map = {self.app.power_name: self.app.power_address}

        # Prepopulate Pebble
        if self.app.pebble_use_emulator and self.pebble_row:
            self.pebble_row.set_subtitle("Emulator mode")
        if self.app.pebble_name and self.pebble_combo:
            self.pebble_combo.append_text(self.app.pebble_name)
            self.pebble_combo.set_active(0)
            self.pebble_map = {self.app.pebble_name: self.app.pebble_address}

        if self.pebble_emu_switch:
            self.pebble_emu_switch.connect("notify::active", self._on_pebble_mode_toggled)
            self._on_pebble_mode_toggled(self.pebble_emu_switch)

        def _set_action_enabled(row: Adw.ActionRow, button: Gtk.Button, enabled: bool):
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

        def _update_actions_state(*_args):
            intervals_athlete_id = (
                (self.icu_id_entry.get_text() or "").strip() if self.icu_id_entry else ""
            )
            intervals_key = (
                (self.icu_key_entry.get_text() or "").strip() if self.icu_key_entry else ""
            )
            database_dsn = (
                (self.dsn_entry.get_text() or "").strip() if self.dsn_entry else ""
            )

            icu_ok = bool(intervals_athlete_id and intervals_key)
            db_ok = bool(database_dsn)

            _set_action_enabled(row_fetch, self.btn_fetch_icu, icu_ok)
            _set_action_enabled(row_upload, self.btn_upload_icu, icu_ok)
            _set_action_enabled(sync_row, self.sync_button, db_ok)

        # Call once for initial state
        _update_actions_state()

        # Recompute whenever the relevant fields change
        self.icu_id_entry.connect("changed", _update_actions_state)
        self.icu_key_entry.connect("changed", _update_actions_state)
        self.dsn_entry.connect("changed", _update_actions_state)

        return scroller

    # ----- Scanners -----
    def _fill_devices_hr(self):
        GLib.idle_add(self.hr_spinner.start)
        self.hr_row.set_subtitle("Scanning for HRM…")

        async def _scan():
            devices = await BleakScanner.discover(
                timeout=5.0, service_uuids=[HEART_RATE_SERVICE_UUID]
            )
            mapping = {d.name: d.address for d in devices if d.name}
            names = sorted(mapping.keys())
            GLib.idle_add(self.hr_spinner.stop)
            GLib.idle_add(self.hr_row.set_subtitle, "" if names else "No HRM found")
            GLib.idle_add(self.hr_combo.remove_all)
            for name in names:
                GLib.idle_add(self.hr_combo.append_text, name)
            if self.app.hr_name and self.app.hr_name in names:
                GLib.idle_add(self.hr_combo.set_active, names.index(self.app.hr_name))
            self.hr_map = mapping

        asyncio.run(_scan())

    def _fill_devices_speed_cadence(self):
        GLib.idle_add(self.speed_spinner.start)
        self.speed_row.set_subtitle("Scanning for speed/cadence devices…")

        async def _scan():
            devices = await discover_speed_cadence_devices(scan_timeout=5.0)
            mapping = {d.name: d.address for d in devices if d.name}
            names = sorted(mapping.keys())

            def _apply():
                self.speed_spinner.stop()
                self.speed_row.set_subtitle("" if names else "No speed devices found")

                # Speed
                self.speed_combo.remove_all()
                for name in names:
                    self.speed_combo.append_text(name)
                self.speed_map = mapping
                if self.app.speed_name and self.app.speed_name in names:
                    self.speed_combo.set_active(names.index(self.app.speed_name))

                # Cadence
                self.cadence_spinner.stop()
                self.cadence_row.set_subtitle("" if names else "No cadence devices found")
                self.cadence_combo.remove_all()
                for name in names:
                    self.cadence_combo.append_text(name)
                self.cadence_map = mapping
                if self.app.cadence_name and self.app.cadence_name in names:
                    self.cadence_combo.set_active(names.index(self.app.cadence_name))

            GLib.idle_add(_apply)

        asyncio.run(_scan())

    def _fill_devices_power(self):
        GLib.idle_add(self.power_spinner.start)
        self.power_row.set_subtitle("Scanning for power devices…")

        async def _scan():
            devices = await discover_power_devices(scan_timeout=5.0)
            mapping = {d.name: d.address for d in devices if d.name}
            names = sorted(mapping.keys())
            GLib.idle_add(self.power_spinner.stop)
            GLib.idle_add(self.power_row.set_subtitle, "" if names else "No power devices found")
            GLib.idle_add(self.power_combo.remove_all)
            for name in names:
                GLib.idle_add(self.power_combo.append_text, name)
            if self.app.power_name and self.app.power_name in names:
                GLib.idle_add(self.power_combo.set_active, names.index(self.app.power_name))
            self.power_map = mapping

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
                    if self.app.pebble_address:
                        for i, disp in enumerate(names):
                            if display_map[disp] == self.app.pebble_address:
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

        out_dir = self.app.workouts_running_dir / "intervals_icu"
        out_dir.mkdir(parents=True, exist_ok=True)

        aid = (self.icu_id_entry.get_text() or "").strip()
        key = (self.icu_key_entry.get_text() or "").strip()
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

                today = datetime.datetime.now(tz=datetime.UTC).date()
                start, end = (today, today + datetime.timedelta(days=6))

                provider.fetch_between("running", start, end, out_dir)

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
        self.app.database_dsn = self.dsn_entry.get_text()

        # HR
        self.app.hr_name = self.hr_combo.get_active_text() or ""
        if self.app.hr_name in self.hr_map:
            self.app.hr_address = self.hr_map[self.app.hr_name]

        # Speed
        self.app.speed_name = self.speed_combo.get_active_text() or ""
        if self.app.speed_name in self.speed_map:
            self.app.speed_address = self.speed_map[self.app.speed_name]

        # Cadence
        self.app.cadence_name = self.cadence_combo.get_active_text() or ""
        if self.app.cadence_name in self.cadence_map:
            self.app.cadence_address = self.cadence_map[self.app.cadence_name]

        # Power
        self.app.power_name = self.power_combo.get_active_text() or ""
        if self.app.power_name in self.power_map:
            self.app.power_address = self.power_map[self.app.power_name]

        # Pebble
        self.app.pebble_enable = (
            self.pebble_enable_row.get_active() if self.pebble_enable_row else False
        )
        self.app.pebble_use_emulator = (
            self.pebble_emu_switch.get_active() if self.pebble_emu_switch else False
        )
        if self.pebble_port_spin:
            self.app.pebble_port = self.pebble_port_spin.get_value_as_int()
        if self.app.pebble_use_emulator:
            self.app.pebble_name = ""
            self.app.pebble_address = ""
        else:
            disp = self.pebble_combo.get_active_text() if self.pebble_combo else ""
            self.app.pebble_name = disp
            self.app.pebble_address = self.pebble_map.get(disp, self.app.pebble_address)

        self.app.resting_hr = self.rest_spin.get_value_as_int()
        self.app.max_hr = self.max_spin.get_value_as_int()
        self.app.ftp_watts = self.ftp_spin.get_value_as_int()

        self.app.icu_athlete_id = self.icu_id_entry.get_text().strip() if self.icu_id_entry else ""
        self.app.icu_api_key = self.icu_key_entry.get_text().strip() if self.icu_key_entry else ""

        cfg = ConfigParser()
        cfg["server"] = {"database_dsn": self.app.database_dsn}
        cfg["sensors_running"] = {
            "hr_name": self.app.hr_name,
            "hr_address": self.app.hr_address,
            "speed_name": self.app.speed_name,
            "speed_address": self.app.speed_address,
            "cadence_name": self.app.cadence_name,
            "cadence_address": self.app.cadence_address,
            "power_name": self.app.power_name,
            "power_address": self.app.power_address,
        }

        cfg["pebble"] = {
            "enable": str(self.app.pebble_enable),
            "use_emulator": str(self.app.pebble_use_emulator),
            "name": self.app.pebble_name or "",
            "mac": self.app.pebble_address or "",
            "port": str(self.app.pebble_port),
        }

        cfg["personal"] = {
            "resting_hr": str(self.app.resting_hr),
            "max_hr": str(self.app.max_hr),
            "ftp_watts": str(self.app.ftp_watts),
        }

        cfg["intervals_icu"] = {
            "athlete_id": self.app.icu_athlete_id,
            "api_key": self.app.icu_api_key,
        }

        with open(self.app.config_file, "w") as f:
            cfg.write(f)

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
            if not self.app.database_dsn:
                GLib.idle_add(self.app.show_toast, "No database DSN configured")
                GLib.idle_add(button.set_sensitive, True)
                return

            try:
                self.app.recorder.db.sync_to_database(self.app.database_dsn)
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
        aid = (self.icu_id_entry.get_text() if self.icu_id_entry else "").strip()
        key = (self.icu_key_entry.get_text() if self.icu_key_entry else "").strip()
        if not key:
            self.app.show_toast("Intervals.icu API key required")
            return
        # Persist to app state so helper can read it
        self.app.icu_athlete_id = aid or "0"
        self.app.icu_api_key = key

        if self.btn_upload_icu:
            self.btn_upload_icu.set_sensitive(False)
        self.app.show_toast("Uploading…")

        def worker():
            try:
                provider = upload_providers.IntervalsICUUploader(athlete_id=aid, api_key=key)
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

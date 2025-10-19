import asyncio
import subprocess
import threading
import requests
from configparser import ConfigParser
from fitness_tracker.upload_providers.intervals_icu import upload_not_uploaded

import gi
from bleak import BleakScanner
from bleaksport.discover import discover_power_devices, discover_speed_cadence_devices

from fitness_tracker.hr_provider import HEART_RATE_SERVICE_UUID

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Adw, GLib, Gtk  # noqa: E402
import contextlib


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
        # General settings group
        prefs_vbox = Adw.PreferencesGroup()
        prefs_vbox.set_title("General Settings")

        # Outer scroller so the page never overflows vertically
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)

        # Database DSN row
        dsn_row = Adw.ActionRow()
        dsn_row.set_title("Database DSN")
        self.dsn_entry = Gtk.Entry()
        self.dsn_entry.set_hexpand(True)
        self.dsn_entry.set_text(self.app.database_dsn)
        dsn_row.add_suffix(self.dsn_entry)
        prefs_vbox.add(dsn_row)

        # ----- Sensors group -----
        dev_group = Adw.PreferencesGroup()
        dev_group.set_title("Sensors")
        # Heart Rate Monitor
        self.hr_row = Adw.ActionRow()
        self.hr_row.set_title("Select HRM")
        self.hr_spinner = Gtk.Spinner()
        self.hr_combo = Gtk.ComboBoxText()
        self.hr_combo.set_hexpand(True)
        self.hr_row.add_prefix(self.hr_spinner)
        self.hr_row.add_suffix(self.hr_combo)
        dev_group.add(self.hr_row)

        hr_scan_row = Adw.ActionRow()
        self.hr_scan_button = Gtk.Button(label="Scan")
        self.hr_scan_button.get_style_context().add_class("suggested-action")
        self.hr_scan_button.connect(
            "clicked",
            lambda _: threading.Thread(target=self._fill_devices_hr, daemon=True).start(),
        )
        hr_scan_row.add_suffix(self.hr_scan_button)
        dev_group.add(hr_scan_row)

        # Speed
        self.speed_row = Adw.ActionRow()
        self.speed_row.set_title("Select Speed Device")
        self.speed_spinner = Gtk.Spinner()
        self.speed_combo = Gtk.ComboBoxText()
        self.speed_combo.set_hexpand(True)
        self.speed_row.add_prefix(self.speed_spinner)
        self.speed_row.add_suffix(self.speed_combo)
        dev_group.add(self.speed_row)

        speed_scan_row = Adw.ActionRow()
        self.speed_scan_button = Gtk.Button(label="Scan")
        self.speed_scan_button.get_style_context().add_class("suggested-action")
        self.speed_scan_button.connect(
            "clicked",
            lambda _: threading.Thread(
                target=self._fill_devices_speed_cadence, daemon=True
            ).start(),
        )
        speed_scan_row.add_suffix(self.speed_scan_button)
        dev_group.add(speed_scan_row)

        # Cadence
        self.cadence_row = Adw.ActionRow()
        self.cadence_row.set_title("Select Cadence Device")
        self.cadence_spinner = Gtk.Spinner()
        self.cadence_combo = Gtk.ComboBoxText()
        self.cadence_combo.set_hexpand(True)
        self.cadence_row.add_prefix(self.cadence_spinner)
        self.cadence_row.add_suffix(self.cadence_combo)
        dev_group.add(self.cadence_row)

        cadence_scan_row = Adw.ActionRow()
        self.cadence_scan_button = Gtk.Button(label="Scan")
        self.cadence_scan_button.get_style_context().add_class("suggested-action")
        self.cadence_scan_button.connect(
            "clicked",
            lambda _: threading.Thread(
                target=self._fill_devices_speed_cadence, daemon=True
            ).start(),
        )
        cadence_scan_row.add_suffix(self.cadence_scan_button)
        dev_group.add(cadence_scan_row)

        # Power
        self.power_row = Adw.ActionRow()
        self.power_row.set_title("Select Power Device")
        self.power_spinner = Gtk.Spinner()
        self.power_combo = Gtk.ComboBoxText()
        self.power_combo.set_hexpand(True)
        self.power_row.add_prefix(self.power_spinner)
        self.power_row.add_suffix(self.power_combo)
        dev_group.add(self.power_row)

        power_scan_row = Adw.ActionRow()
        self.power_scan_button = Gtk.Button(label="Scan")
        self.power_scan_button.get_style_context().add_class("suggested-action")
        self.power_scan_button.connect(
            "clicked",
            lambda _: threading.Thread(target=self._fill_devices_power, daemon=True).start(),
        )
        power_scan_row.add_suffix(self.power_scan_button)
        dev_group.add(power_scan_row)

        # Pebble
        pebble_group = Adw.PreferencesGroup()
        pebble_group.set_title("Pebble Watch")

        # Enable
        self.pebble_enable_row = Adw.SwitchRow()
        self.pebble_enable_row.set_title("Enable Pebble")
        self.pebble_enable_row.set_active(self.app.pebble_enable)
        pebble_group.add(self.pebble_enable_row)

        # Emulator vs Watch
        self.pebble_emu_switch = Adw.SwitchRow()
        self.pebble_emu_switch.set_title("Use Emulator")
        self.pebble_emu_switch.set_active(self.app.pebble_use_emulator)
        pebble_group.add(self.pebble_emu_switch)

        self.pebble_row = Adw.ActionRow()
        self.pebble_row.set_title("Pebble")
        self.pebble_spinner = Gtk.Spinner()
        self.pebble_combo = Gtk.ComboBoxText()
        self.pebble_combo.set_hexpand(False)
        self.pebble_combo.set_size_request(240, -1)
        self.pebble_combo.set_halign(Gtk.Align.END)
        self.pebble_combo.connect("changed", self._on_pebble_combo_changed)
        self.pebble_row.add_prefix(self.pebble_spinner)
        self.pebble_row.add_suffix(self.pebble_combo)
        if hasattr(self.pebble_row, "set_title_lines"):
            self.pebble_row.set_title_lines(1)
        pebble_group.add(self.pebble_row)

        # Emulator port (only visible when using emulator)
        self.pebble_port_row = Adw.ActionRow()
        self.pebble_port_row.set_title("Emulator Port")
        self.pebble_port_spin = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.pebble_port_spin.set_value(self.app.pebble_port or 47527)
        self.pebble_port_spin.set_hexpand(False)
        self.pebble_port_spin.set_width_chars(6)
        self.pebble_port_row.add_suffix(self.pebble_port_spin)
        pebble_group.add(self.pebble_port_row)

        self.pebble_scan_row = Adw.ActionRow()
        self.pebble_scan_row.set_title("Scan Pebble")
        self.pebble_scan_button = Gtk.Button(label="Scan")
        self.pebble_scan_button.get_style_context().add_class("suggested-action")
        self.pebble_scan_button.connect(
            "clicked",
            lambda _b: threading.Thread(target=self._fill_devices_pebble, daemon=True).start(),
        )
        self.pebble_scan_row.add_suffix(self.pebble_scan_button)
        pebble_group.add(self.pebble_scan_row)

        # Personal info group (for HR, weight, height, etc.)
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

        # --- Intervals.icu provider ---
        icu_group = Adw.PreferencesGroup()
        icu_group.set_title("Intervals.icu")

        row_icu_id = Adw.ActionRow()
        row_icu_id.set_title("Athlete ID")
        self.icu_id_entry = Gtk.Entry()
        self.icu_id_entry.set_hexpand(True)
        self.icu_id_entry.set_text(self.app.icu_athlete_id or "")
        row_icu_id.add_suffix(self.icu_id_entry)
        icu_group.add(row_icu_id)

        row_icu_key = Adw.ActionRow()
        row_icu_key.set_title("API Key")
        self.icu_key_entry = Gtk.Entry()
        self.icu_key_entry.set_visibility(False)  # hide text (password-like)
        self.icu_key_entry.set_hexpand(True)
        self.icu_key_entry.set_text(self.app.icu_api_key or "")
        row_icu_key.add_suffix(self.icu_key_entry)
        icu_group.add(row_icu_key)

        row_fetch = Adw.ActionRow()
        row_fetch.set_title("Fetch Week's Running Workouts")
        self.btn_fetch_icu = Gtk.Button(label="Fetch")
        self.btn_fetch_icu.get_style_context().add_class("suggested-action")
        self.btn_fetch_icu.connect("clicked", self._on_fetch_icu)
        row_fetch.add_suffix(self.btn_fetch_icu)
        icu_group.add(row_fetch)

        row_upload = Adw.ActionRow()
        row_upload.set_title("Upload completed workouts")
        self.btn_upload_icu = Gtk.Button(label="Upload")
        self.btn_upload_icu.get_style_context().add_class("suggested-action")
        self.btn_upload_icu.connect("clicked", self._on_upload_icu)
        row_upload.add_suffix(self.btn_upload_icu)
        icu_group.add(row_upload)


        # Save Button
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

        # Sync button
        sync_row = Adw.ActionRow()
        sync_row.set_title("Sync to Database")
        sync_row.set_activatable(True)
        self.sync_button = Gtk.Button(label="Sync")
        self.sync_button.get_style_context().add_class("suggested-action")
        self.sync_button.connect("clicked", self._on_sync)
        sync_row.add_suffix(self.sync_button)
        action_group.add(sync_row)

        # Layout container
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        container.set_margin_top(12)
        container.set_margin_bottom(12)
        container.set_margin_start(12)
        container.set_margin_end(12)
        container.append(prefs_vbox)
        container.append(dev_group)
        container.append(pebble_group)
        container.append(personal_group)
        container.append(icu_group)
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
        if self.app.pebble_use_emulator:
            self.pebble_row.set_subtitle("Emulator mode")
        if self.app.pebble_name and self.pebble_combo:
            self.pebble_combo.append_text(self.app.pebble_name)
            self.pebble_combo.set_active(0)
            self.pebble_map = {self.app.pebble_name: self.app.pebble_address}

        # Hide/show Pebble BT rows based on emulator switch to reduce vertical size
        self.pebble_emu_switch.connect("notify::active", self._on_pebble_mode_toggled)
        self._on_pebble_mode_toggled(self.pebble_emu_switch)

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
            try:
                name_to_mac = _scan_cli()
            except Exception as e:
                name_to_mac = {}
                GLib.idle_add(self.pebble_row.set_subtitle, f"Scan failed: {e}")

            display_map = _uniq_display_names(name_to_mac)
            names = sorted(display_map.keys())

            def _update_ui():
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
        from datetime import date
        from fitness_tracker.workout_providers import week_window_from
        from fitness_tracker.workout_providers.intervals_icu import IntervalsICUProvider

        aid = (self.icu_id_entry.get_text() or "").strip()
        key = (self.icu_key_entry.get_text() or "").strip()
        if not (aid and key):
            self.app.show_toast("Intervals.icu Athlete ID and API key required")
            return

        self.btn_fetch_icu.set_sensitive(False)

        def worker():
            try:
                provider = IntervalsICUProvider(athlete_id=aid, api_key=key, ext="fit")
                start, end = week_window_from(date.today())
                out_dir = self.app.workouts_running_provider_dir
                n = 0
                for _dw in provider.fetch_between("running", start, end, out_dir):
                    n += 1
                # simply refresh the existing list
                GLib.idle_add(self.app.tracker.mode_view.refresh)
            except requests.HTTPError as e:
                GLib.idle_add(self.app.show_toast, f"Intervals.icu error: {e.response.status_code}")
            except Exception as e:
                GLib.idle_add(self.app.show_toast, f"Fetch failed: {e}")
            finally:
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
        self.app.pebble_enable = self.pebble_enable_row.get_active()
        self.app.pebble_use_emulator = self.pebble_emu_switch.get_active()
        if self.pebble_port_spin:
            self.app.pebble_port = self.pebble_port_spin.get_value_as_int()
        if self.app.pebble_use_emulator:
            self.app.pebble_name = ""
            self.app.pebble_address = ""
        else:
            disp = self.pebble_combo.get_active_text() or ""
            self.app.pebble_name = disp
            self.app.pebble_address = self.pebble_map.get(disp, self.app.pebble_address)

        self.app.resting_hr = self.rest_spin.get_value_as_int()
        self.app.max_hr = self.max_spin.get_value_as_int()
        self.app.ftp_watts = self.ftp_spin.get_value_as_int()

        self.app.icu_athlete_id = self.icu_id_entry.get_text().strip()
        self.app.icu_api_key = self.icu_key_entry.get_text().strip()

        cfg = ConfigParser()
        cfg["server"] = {"database_dsn": self.app.database_dsn}
        cfg["sensors"] = {
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
        aid = (self.icu_id_entry.get_text() or "").strip()
        key = (self.icu_key_entry.get_text() or "").strip()
        if not key:
            self.app.show_toast("Intervals.icu API key required")
            return
        # Persist to app state so helper can read it
        self.app.icu_athlete_id = aid or "0"
        self.app.icu_api_key = key

        self.btn_upload_icu.set_sensitive(False)
        self.app.show_toast("Uploading…")

        def worker():
            try:
                results = upload_not_uploaded(self.app)
                ok = sum(1 for _, s, _ in results if s)
                fail = [err for _, s, err in results if not s]
                if ok:
                    GLib.idle_add(self.app.show_toast, f"✅ Uploaded {ok} new {"activies" if ok > 1 else "activity"}")
                if fail:
                    GLib.idle_add(self.app.show_toast, f"⚠️ {len(fail)} failed")
            except Exception as e:
                GLib.idle_add(self.app.show_toast, f"Upload failed: {e}")
            finally:
                GLib.idle_add(self.btn_upload_icu.set_sensitive, True)

        threading.Thread(target=worker, daemon=True).start()

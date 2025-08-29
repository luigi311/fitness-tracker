import asyncio
import threading
from configparser import ConfigParser

import gi
from bleak import BleakScanner
from bleaksport.discover import discover_running_devices

from fitness_tracker.hr_provider import HEART_RATE_SERVICE_UUID

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Adw, GLib, Gtk  # noqa: E402


class SettingsPageUI:
    def __init__(self, app: "FitnessAppUI"):
        self.app = app

        # HR
        self.device_map: dict[str, str] = {}
        # running
        self.running_map: dict[str, str] = {}

    def build_page(self) -> Gtk.Widget:
        # General settings group
        prefs_vbox = Adw.PreferencesGroup()
        prefs_vbox.set_title("General Settings")

        # Database DSN row
        dsn_row = Adw.ActionRow()
        dsn_row.set_title("Database DSN")
        self.dsn_entry = Gtk.Entry()
        self.dsn_entry.set_hexpand(True)
        self.dsn_entry.set_text(self.app.database_dsn)
        dsn_row.add_suffix(self.dsn_entry)
        prefs_vbox.add(dsn_row)

        # ----- Heart-rate device group -----
        dev_group = Adw.PreferencesGroup()
        dev_group.set_title("Heart Rate Monitor")
        self.device_row = Adw.ActionRow()
        self.device_row.set_title("Select HRM")
        self.device_spinner = Gtk.Spinner()
        if not self.app.device_name:
            self.device_spinner.start()
        self.device_combo = Gtk.ComboBoxText()
        self.device_combo.set_hexpand(True)
        self.device_row.add_prefix(self.device_spinner)
        self.device_row.add_suffix(self.device_combo)
        dev_group.add(self.device_row)

        rescan_row = Adw.ActionRow()
        rescan_row.set_title("Rescan HRM")
        self.rescan_button = Gtk.Button(label="Rescan")
        self.rescan_button.get_style_context().add_class("suggested-action")
        self.rescan_button.connect(
            "clicked",
            lambda _: threading.Thread(target=self._fill_devices_hr, daemon=True).start(),
        )
        rescan_row.add_suffix(self.rescan_button)
        dev_group.add(rescan_row)

        # ----- Running device group (RSCS / Stryd CPS) -----
        run_group = Adw.PreferencesGroup()
        run_group.set_title("Running Device")

        self.run_row = Adw.ActionRow()
        self.run_row.set_title("Select Running Device")
        self.run_spinner = Gtk.Spinner()
        if not self.app.running_device_name:
            self.run_spinner.start()
        self.run_combo = Gtk.ComboBoxText()
        self.run_combo.set_hexpand(True)
        self.run_row.add_prefix(self.run_spinner)
        self.run_row.add_suffix(self.run_combo)
        run_group.add(self.run_row)

        run_rescan_row = Adw.ActionRow()
        run_rescan_row.set_title("Rescan Running Devices")
        self.run_rescan_button = Gtk.Button(label="Rescan")
        self.run_rescan_button.get_style_context().add_class("suggested-action")
        self.run_rescan_button.connect(
            "clicked",
            lambda _: threading.Thread(target=self._fill_devices_running, daemon=True).start(),
        )
        run_rescan_row.add_suffix(self.run_rescan_button)
        run_group.add(run_rescan_row)

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
        container.append(run_group)
        container.append(personal_group)
        container.append(action_group)

        # Prepopulate HRM
        if self.app.device_name:
            self.device_spinner.stop()
            self.device_combo.append_text(self.app.device_name)
            self.device_combo.set_active(0)
            self.device_map = {self.app.device_name: self.app.device_address}
        else:
            threading.Thread(target=self._fill_devices_hr, daemon=True).start()

        # Prepopulate Running
        if self.app.running_device_name:
            self.run_spinner.stop()
            self.run_combo.append_text(self.app.running_device_name)
            self.run_combo.set_active(0)
            self.running_map = {self.app.running_device_name: self.app.running_device_address}
        else:
            threading.Thread(target=self._fill_devices_running, daemon=True).start()

        return container

    # ----- Scanners -----
    def _fill_devices_hr(self):
        GLib.idle_add(self.device_spinner.start)
        self.device_row.set_subtitle("Scanning for HRM…")

        async def _scan():
            devices = await BleakScanner.discover(timeout=5.0, service_uuids=[HEART_RATE_SERVICE_UUID])
            mapping = {d.name: d.address for d in devices if d.name}
            names = sorted(mapping.keys())
            GLib.idle_add(self.device_spinner.stop)
            GLib.idle_add(self.device_row.set_subtitle, "" if names else "No HRM found")
            GLib.idle_add(self.device_combo.remove_all)
            for name in names: GLib.idle_add(self.device_combo.append_text, name)
            if self.app.device_name and self.app.device_name in names:
                GLib.idle_add(self.device_combo.set_active, names.index(self.app.device_name))
            self.device_map = mapping
        asyncio.run(_scan())

    def _fill_devices_running(self):
        GLib.idle_add(self.run_spinner.start)
        self.run_row.set_subtitle("Scanning for running devices…")

        async def _scan():
            devices = await discover_running_devices(timeout=5.0)
            mapping = {d.name: d.address for d in devices if d.name}
            names = sorted(mapping.keys())
            GLib.idle_add(self.run_spinner.stop)
            GLib.idle_add(self.run_row.set_subtitle, "" if names else "No running devices found")
            GLib.idle_add(self.run_combo.remove_all)
            for name in names: GLib.idle_add(self.run_combo.append_text, name)
            if self.app.running_device_name and self.app.running_device_name in names:
                GLib.idle_add(self.run_combo.set_active, names.index(self.app.running_device_name))
            self.running_map = mapping
        asyncio.run(_scan())

    def _on_save_settings(self, _button):
        self.app.database_dsn = self.dsn_entry.get_text()

        # HR
        self.app.device_name = self.device_combo.get_active_text() or ""
        if self.app.device_name in self.device_map:
            self.app.device_address = self.device_map[self.app.device_name]

        # Running
        self.app.running_device_name = self.run_combo.get_active_text() or ""
        if self.app.running_device_name in self.running_map:
            self.app.running_device_address = self.running_map[self.app.running_device_name]

        self.app.resting_hr = self.rest_spin.get_value_as_int()
        self.app.max_hr = self.max_spin.get_value_as_int()

        cfg = ConfigParser()
        cfg["server"] = {"database_dsn": self.app.database_dsn}
        cfg["tracker"] = {"device_name": self.app.device_name, "device_address": self.app.device_address}
        cfg["running"] = {  # NEW
            "device_name": self.app.running_device_name,
            "device_address": self.app.running_device_address,
        }
        cfg["personal"] = {"resting_hr": str(self.app.resting_hr), "max_hr": str(self.app.max_hr)}

        with open(self.app.config_file, "w") as f:
            cfg.write(f)

        toast = Adw.Toast.new("Settings saved successfully")
        GLib.idle_add(self.app.toast_overlay.add_toast, toast)

        GLib.idle_add(self.app.tracker.fig.canvas.draw_idle)
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

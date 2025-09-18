import asyncio
import subprocess
import threading
from configparser import ConfigParser

import gi
from bleak import BleakScanner
from bleaksport.discover import discover_running_devices

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
        self.pebble_rescan_row: Adw.ActionRow | None = None
        self.pebble_spinner: Gtk.Spinner | None = None
        self.pebble_combo: Gtk.ComboBoxText | None = None
        self.pebble_port_row: Adw.ActionRow | None = None
        self.pebble_port_spin: Gtk.SpinButton | None = None

        # HR
        self.device_map: dict[str, str] = {}
        # running
        self.running_map: dict[str, str] = {}
        # pebble
        self.pebble_map: dict[str, str] = {}

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

        # Rescan button
        self.pebble_rescan_row = Adw.ActionRow()
        self.pebble_rescan_row.set_title("Rescan Pebble")
        self.pebble_rescan_button = Gtk.Button(label="Rescan")
        self.pebble_rescan_button.get_style_context().add_class("suggested-action")
        self.pebble_rescan_button.connect(
            "clicked",
            lambda _b: threading.Thread(target=self._fill_devices_pebble, daemon=True).start(),
        )
        self.pebble_rescan_row.add_suffix(self.pebble_rescan_button)
        pebble_group.add(self.pebble_rescan_row)

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
        container.append(pebble_group)
        container.append(personal_group)
        container.append(action_group)
        # Return the scroller so the page scrolls on small windows
        scroller.set_child(container)

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

        # Prepopulate Pebble
        if self.app.pebble_use_emulator:
            self.pebble_row.set_subtitle("Emulator mode")
        else:
            threading.Thread(target=self._fill_devices_pebble, daemon=True).start()

        # Hide/show Pebble BT rows based on emulator switch to reduce vertical size
        self.pebble_emu_switch.connect("notify::active", self._on_pebble_mode_toggled)
        self._on_pebble_mode_toggled(self.pebble_emu_switch)

        return scroller

    # ----- Scanners -----
    def _fill_devices_hr(self):
        GLib.idle_add(self.device_spinner.start)
        self.device_row.set_subtitle("Scanning for HRM…")

        async def _scan():
            devices = await BleakScanner.discover(
                timeout=5.0, service_uuids=[HEART_RATE_SERVICE_UUID]
            )
            mapping = {d.name: d.address for d in devices if d.name}
            names = sorted(mapping.keys())
            GLib.idle_add(self.device_spinner.stop)
            GLib.idle_add(self.device_row.set_subtitle, "" if names else "No HRM found")
            GLib.idle_add(self.device_combo.remove_all)
            for name in names:
                GLib.idle_add(self.device_combo.append_text, name)
            if self.app.device_name and self.app.device_name in names:
                GLib.idle_add(self.device_combo.set_active, names.index(self.app.device_name))
            self.device_map = mapping

        asyncio.run(_scan())

    def _fill_devices_running(self):
        GLib.idle_add(self.run_spinner.start)
        self.run_row.set_subtitle("Scanning for running devices…")

        async def _scan():
            devices = await discover_running_devices(scan_timeout=5.0)
            mapping = {d.name: d.address for d in devices if d.name}
            names = sorted(mapping.keys())
            GLib.idle_add(self.run_spinner.stop)
            GLib.idle_add(self.run_row.set_subtitle, "" if names else "No running devices found")
            GLib.idle_add(self.run_combo.remove_all)
            for name in names:
                GLib.idle_add(self.run_combo.append_text, name)
            if self.app.running_device_name and self.app.running_device_name in names:
                GLib.idle_add(self.run_combo.set_active, names.index(self.app.running_device_name))
            self.running_map = mapping

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
                    if self.app.pebble_mac:
                        for i, disp in enumerate(names):
                            if display_map[disp] == self.app.pebble_mac:
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
        if self.pebble_rescan_row:
            self.pebble_rescan_row.set_visible(not use_emu)
        if self.pebble_port_row:
            self.pebble_port_row.set_visible(use_emu)
        return False

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

        # Pebble
        self.app.pebble_enable = self.pebble_enable_row.get_active()
        self.app.pebble_use_emulator = self.pebble_emu_switch.get_active()
        if self.pebble_port_spin:
            self.app.pebble_port = self.pebble_port_spin.get_value_as_int()
        if self.app.pebble_use_emulator:
            self.app.pebble_mac = ""
        else:
            disp = self.pebble_combo.get_active_text() or ""
            self.app.pebble_mac = self.pebble_map.get(disp, self.app.pebble_mac)

        self.app.resting_hr = self.rest_spin.get_value_as_int()
        self.app.max_hr = self.max_spin.get_value_as_int()

        cfg = ConfigParser()
        cfg["server"] = {"database_dsn": self.app.database_dsn}
        cfg["tracker"] = {
            "device_name": self.app.device_name,
            "device_address": self.app.device_address,
        }
        cfg["running"] = {
            "device_name": self.app.running_device_name,
            "device_address": self.app.running_device_address,
        }

        cfg["pebble"] = {
            "enable": str(self.app.pebble_enable),
            "use_emulator": str(self.app.pebble_use_emulator),
            "mac": self.app.pebble_mac or "",
            "port": str(self.app.pebble_port),
        }

        cfg["personal"] = {"resting_hr": str(self.app.resting_hr), "max_hr": str(self.app.max_hr)}

        with open(self.app.config_file, "w") as f:
            cfg.write(f)

        # Apply Pebble settings right away (start/stop bridge without restart)
        GLib.idle_add(self.app.apply_pebble_settings)

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

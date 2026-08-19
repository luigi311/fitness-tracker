import asyncio
import contextlib
import threading
from collections.abc import Callable, Coroutine, Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import gi
from bleaksport import (
    discover_ftms_devices,
    discover_heart_rate_devices,
    discover_power_devices,
    discover_speed_cadence_devices,
)
from dbus_fast import BusType
from dbus_fast.aio import MessageBus
from loguru import logger

from fitness_tracker.core.settings import AppSettings
from fitness_tracker.services.intervals_icu import (
    IntegrationError,
    refresh_intervals_workouts,
    upload_intervals_activities,
)
from fitness_tracker.services.jobs import CancellationToken, DuplicateJobError
from fitness_tracker.ui.pages.settings.sections import (
    ActionSection,
    DevicesSection,
    DisplaySection,
    LocationSection,
    PebbleSection,
    PersonalSection,
    ProviderSection,
    SensorAddressMap,
    SensorRowSpec,
    SensorRowWidgets,
    SensorSection,
    TrainerDeviceMap,
)

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Adw, GLib, Gtk  # noqa: E402  # ty:ignore[unresolved-import]


def _idle_once(callback: Callable[[], object]) -> None:
    """Schedule a callback that is always removed after its first invocation."""

    def invoke() -> bool:
        callback()
        return False

    GLib.idle_add(invoke)


if TYPE_CHECKING:
    from fitness_tracker.ui.app import FitnessAppUI


class SettingsPageUI:
    """Build and coordinate the settings page sections and scanners."""

    def __init__(self, app: "FitnessAppUI") -> None:
        self.app = app
        # Bleak keeps one BlueZ manager per event loop. Serialize the short-lived
        # settings loops so a burst of button clicks cannot create overlapping
        # managers and scans.
        self._ble_scan_lock = threading.Lock()

        self.personal_section: PersonalSection | None = None
        self.display_section: DisplaySection | None = None
        self.location_section: LocationSection | None = None
        self.devices_section: DevicesSection | None = None
        self.pebble_section: PebbleSection | None = None
        self.provider_section: ProviderSection | None = None
        self.actions_section: ActionSection | None = None
        self.sensor_sections: dict[str, SensorSection] = {}

    def _make_sensor_sections(self) -> dict[str, SensorSection]:
        """Create the sensor sections and their scanner-group declarations."""
        sensor_specs = (
            SensorRowSpec(
                key="hr",
                title="Select HRM",
                scan_label="Scan HRM",
                scanner=self._fill_devices_hr,
                scan_group="hr",
                settings_field="hr_name",
            ),
            SensorRowSpec(
                key="speed",
                title="Select Speed Device",
                scan_label="Scan Speed",
                scanner=self._fill_devices_speed_cadence,
                scan_group="speed_cadence",
                settings_field="speed_name",
            ),
            SensorRowSpec(
                key="cadence",
                title="Select Cadence Device",
                scan_label="Scan Cadence",
                scanner=self._fill_devices_speed_cadence,
                scan_group="speed_cadence",
                settings_field="cadence_name",
            ),
            SensorRowSpec(
                key="power",
                title="Select Power Device",
                scan_label="Scan Power",
                scanner=self._fill_devices_power,
                scan_group="power",
                settings_field="power_name",
            ),
        )
        return {
            "running": SensorSection(
                title="Running Sensors",
                subtitle="Heart rate, speed, cadence, power",
                settings=self.app.app_settings.running_sensors,
                specs=sensor_specs,
            ),
            "cycling": SensorSection(
                title="Cycling Sensors",
                subtitle="Heart rate, speed, cadence, power",
                settings=self.app.app_settings.cycling_sensors,
                specs=sensor_specs,
            ),
            "trainer_running": SensorSection(
                title="Running Trainer",
                subtitle="Treadmill with FTMS support",
                settings=self.app.app_settings.trainer_running,
                specs=(
                    SensorRowSpec(
                        key="trainer",
                        title="Trainer",
                        scan_label="Scan Trainer",
                        scanner=self._fill_devices_running_trainer,
                        scan_group="trainer_running",
                        settings_field="trainer_name",
                    ),
                    SensorRowSpec(
                        key="hr",
                        title="Trainer HRM",
                        scan_label="Scan HRM",
                        scanner=self._fill_devices_running_trainer_hr,
                        scan_group="trainer_running_hr",
                        settings_field="hr_name",
                    ),
                ),
            ),
            "trainer_cycling": SensorSection(
                title="Cycling Trainer",
                subtitle="Smart Trainer / Indoor Bike",
                settings=self.app.app_settings.trainer_cycling,
                specs=(
                    SensorRowSpec(
                        key="trainer",
                        title="Trainer",
                        scan_label="Scan Trainer",
                        scanner=self._fill_devices_cycling_trainer,
                        scan_group="trainer_cycling",
                        settings_field="trainer_name",
                    ),
                    SensorRowSpec(
                        key="hr",
                        title="Trainer HRM",
                        scan_label="Scan HRM",
                        scanner=self._fill_devices_cycling_trainer_hr,
                        scan_group="trainer_cycling_hr",
                        settings_field="hr_name",
                    ),
                ),
            ),
        }

    def build_page(self) -> Gtk.Widget:
        """Build the scrollable settings page from its composable sections."""
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)

        self.personal_section = PersonalSection(self.app.app_settings.personal)
        self.display_section = DisplaySection(self.app.app_settings.display)
        self.location_section = LocationSection(self.app.app_settings.location)
        self.sensor_sections = self._make_sensor_sections()
        self.devices_section = DevicesSection(self.sensor_sections)
        self.pebble_section = PebbleSection(
            self.app.app_settings.pebble,
            on_scan=self._fill_devices_pebble,
        )
        self.provider_section = ProviderSection(
            self.app.app_settings.icu,
            self.app.app_settings.database,
        )
        self.actions_section = ActionSection(
            icu_configured=self.provider_section.icu_configured,
            database_configured=self.provider_section.database_configured,
            on_save=self._on_save_settings,
            on_fetch=self._on_fetch_icu,
            on_upload=self._on_upload_icu,
            on_sync=self._on_sync,
        )

        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        container.set_margin_top(12)
        container.set_margin_bottom(12)
        container.set_margin_start(4)
        container.set_margin_end(4)
        container.append(self.personal_section.build())
        container.append(self.display_section.build())
        container.append(self.location_section.build())
        container.append(self.devices_section.build())
        container.append(self.pebble_section.build())
        container.append(self.provider_section.build())
        container.append(self.actions_section.build())
        scroller.set_child(container)

        for section in self.sensor_sections.values():
            section.load()
        self._update_actions_state()
        return scroller

    def _update_actions_state(self, *_args: object) -> None:
        if self.provider_section is None or self.actions_section is None:
            return
        self.actions_section.set_enabled(
            icu_configured=self.provider_section.icu_configured,
            database_configured=self.provider_section.database_configured,
        )

    # ----- Scanners -----
    def _run_ble_scan[T](
        self,
        scan_factory: Callable[[], Coroutine[Any, Any, T]],
    ) -> T:
        """Run one scan while holding the BLE lock for its full five-second window.

        The job runner creates a thread per submitted job, so a waiting scan
        does not prevent unrelated background work from starting.
        """
        with self._ble_scan_lock:
            return asyncio.run(scan_factory())

    @staticmethod
    async def _scan_heart_rate_devices() -> dict[str, str]:
        devices = await discover_heart_rate_devices(scan_timeout=5.0)
        return {device.name: device.address for device in devices if device.name}

    def _submit_scan[T](
        self,
        name: str,
        scan_factory: Callable[[], Coroutine[Any, Any, T]],
        *,
        on_start: Callable[[], None],
        on_success: Callable[[T], None],
        on_error: Callable[[Exception], None],
        on_finally: Callable[[], None],
    ) -> bool:
        def work(token: CancellationToken) -> T:
            token.raise_if_cancelled()
            result = self._run_ble_scan(scan_factory)
            token.raise_if_cancelled()
            return result

        try:
            self.app.jobs.submit(
                name,
                work,
                on_success=on_success,
                on_error=on_error,
                on_finally=on_finally,
            )
        except DuplicateJobError:
            logger.debug("{} is already running", name)
            return False
        on_start()
        return True

    def _submit_sensor_scan(
        self,
        *,
        name: str,
        scan_group: str,
        scan_factory: Callable[
            [],
            Coroutine[Any, Any, SensorAddressMap | TrainerDeviceMap],
        ],
        scanning_message: str,
        empty_message: str | Mapping[str, str],
    ) -> bool:
        """Run a sensor scan with shared widget lifecycle handling."""

        def on_success(mapping: SensorAddressMap | TrainerDeviceMap) -> None:
            self._apply_scan_result(scan_group, mapping, empty_message)

        def on_error(error: Exception) -> None:
            message = f"Scan failed: {error}"
            for widget in self._scan_widgets(scan_group):
                widget.row.set_subtitle(message)
            self.app.show_toast(message)

        def on_start() -> None:
            for widget in self._scan_widgets(scan_group):
                widget.spinner.start()
                widget.row.set_subtitle(scanning_message)

        def on_finally() -> None:
            for widget in self._scan_widgets(scan_group):
                widget.spinner.stop()

        return self._submit_scan(
            name,
            scan_factory,
            on_start=on_start,
            on_success=on_success,
            on_error=on_error,
            on_finally=on_finally,
        )

    def _scan_widgets(self, scan_group: str) -> tuple[SensorRowWidgets, ...]:
        return tuple(
            widget
            for section in self.sensor_sections.values()
            for widget in section.scan_widgets(scan_group)
        )

    def _apply_scan_result(
        self,
        scan_group: str,
        mapping: SensorAddressMap | TrainerDeviceMap,
        empty_message: str | Mapping[str, str],
    ) -> None:
        for section in self.sensor_sections.values():
            section.apply_scan_result(scan_group, mapping, empty_message)

    def _fill_devices_hr(self) -> None:
        self._submit_sensor_scan(
            name="ble-scan-hr",
            scan_group="hr",
            scan_factory=self._scan_heart_rate_devices,
            scanning_message="Scanning for HRM…",
            empty_message="No HRM found",
        )

    def _fill_devices_speed_cadence(self) -> None:
        async def _scan() -> dict[str, str]:
            devices = await discover_speed_cadence_devices(scan_timeout=5.0)
            return {d.name: d.address for d in devices if d.name}

        self._submit_sensor_scan(
            name="ble-scan-speed-cadence",
            scan_group="speed_cadence",
            scan_factory=_scan,
            scanning_message="Scanning for speed/cadence devices…",
            empty_message={
                "speed": "No speed devices found",
                "cadence": "No cadence devices found",
            },
        )

    def _fill_devices_power(self) -> None:
        async def _scan() -> dict[str, str]:
            devices = await discover_power_devices(scan_timeout=5.0)
            return {d.name: d.address for d in devices if d.name}

        self._submit_sensor_scan(
            name="ble-scan-power",
            scan_group="power",
            scan_factory=_scan,
            scanning_message="Scanning for power devices…",
            empty_message="No power devices found",
        )

    def _fill_devices_trainer_hr(self, scan_group: str, job_name: str) -> None:
        self._submit_sensor_scan(
            name=job_name,
            scan_group=scan_group,
            scan_factory=self._scan_heart_rate_devices,
            scanning_message="Scanning for HRM…",
            empty_message="No HRM found",
        )

    def _fill_devices_running_trainer_hr(self) -> None:
        self._fill_devices_trainer_hr(
            "trainer_running_hr",
            "ble-scan-trainer-running-hr",
        )

    def _fill_devices_cycling_trainer_hr(self) -> None:
        self._fill_devices_trainer_hr(
            "trainer_cycling_hr",
            "ble-scan-trainer-cycling-hr",
        )

    def _fill_devices_trainer(self, scan_group: str, job_name: str) -> None:
        async def _scan() -> TrainerDeviceMap:
            found = await discover_ftms_devices(scan_timeout=5.0)
            logger.debug(f"Found FTMS devices: {found}")

            mapping: TrainerDeviceMap = {}
            for dev, mtype in found:
                name = getattr(dev, "name", None) or "(unnamed)"
                addr = getattr(dev, "address", None) or ""
                disp = f"{name} [{addr}]"
                logger.debug(f"Mapping trainer: {disp} -> {addr} ({mtype})")
                mapping[disp] = {"address": addr, "machine_type": mtype}
            return mapping

        self._submit_sensor_scan(
            name=job_name,
            scan_group=scan_group,
            scan_factory=_scan,
            scanning_message="Scanning for FTMS trainers…",
            empty_message="No FTMS trainers found",
        )

    def _fill_devices_running_trainer(self) -> None:
        self._fill_devices_trainer(
            "trainer_running",
            "ble-scan-trainer-running",
        )

    def _fill_devices_cycling_trainer(self) -> None:
        self._fill_devices_trainer(
            "trainer_cycling",
            "ble-scan-trainer-cycling",
        )

    def _fill_devices_pebble(self) -> None:
        section = self.pebble_section
        if section is None:
            return

        async def _scan() -> list[tuple[str, str]]:
            devices: list[tuple[str, str]] = []
            bus = MessageBus(bus_type=BusType.SYSTEM)
            try:
                await bus.connect()
                introspection = await bus.introspect("org.bluez", "/")
                proxy = bus.get_proxy_object("org.bluez", "/", introspection)
                obj_manager = proxy.get_interface("org.freedesktop.DBus.ObjectManager")
                managed_objects = await cast("Any", obj_manager).call_get_managed_objects()
                for interfaces in managed_objects.values():
                    device_iface = interfaces.get("org.bluez.Device1")
                    if not device_iface:
                        continue
                    name_v = device_iface.get("Name")
                    addr_v = device_iface.get("Address")
                    if not name_v or not addr_v:
                        continue
                    name, mac = name_v.value, addr_v.value
                    if "pebble" in name.lower():
                        devices.append((name, mac))
            finally:
                with contextlib.suppress(Exception):
                    bus.disconnect()
                with contextlib.suppress(Exception):
                    await bus.wait_for_disconnect()
            return devices

        def on_success(devices: list[tuple[str, str]]) -> None:
            section.set_scan_results(devices)

        def on_error(error: Exception) -> None:
            message = f"Scan failed: {error}"
            section.set_scan_error(message)
            self.app.show_toast(message)

        self._submit_scan(
            "ble-scan-pebble",
            _scan,
            on_start=section.set_scan_started,
            on_success=on_success,
            on_error=on_error,
            on_finally=section.set_scan_finished,
        )

    def _on_fetch_icu(self, _button: Gtk.Button) -> None:
        if self.actions_section is None or self.actions_section.fetch_button is None:
            return
        fetch_button = self.actions_section.fetch_button

        out_dir_running = self.app.workouts_running_dir / "intervals_icu"
        out_dir_running.mkdir(parents=True, exist_ok=True)

        out_dir_cycling = self.app.workouts_cycling_dir / "intervals_icu"
        out_dir_cycling.mkdir(parents=True, exist_ok=True)

        aid = (self.app.app_settings.icu.athlete_id or "").strip()
        key = (self.app.app_settings.icu.api_key or "").strip()

        fetch_button.set_sensitive(False)

        def work(_token: CancellationToken) -> object:
            start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
            end = (start + timedelta(days=6)).replace(hour=23, minute=59, second=59)
            return refresh_intervals_workouts(
                athlete_id=aid,
                api_key=key,
                start=start,
                end=end,
                running_dir=out_dir_running,
                cycling_dir=out_dir_cycling,
            )

        def on_success(_result: object) -> None:
            self.app.tracker.mode_view.refresh()

        def on_error(error: Exception) -> None:
            if isinstance(error, IntegrationError):
                self.app.show_toast(f"Intervals.icu error: {error}")
            else:
                self.app.show_toast(f"Fetch failed: {error}")

        def on_finally() -> None:
            fetch_button.set_sensitive(True)

        try:
            self.app.jobs.submit(
                "intervals-fetch",
                work,
                on_success=on_success,
                on_error=on_error,
                on_finally=on_finally,
            )
        except DuplicateJobError:
            logger.debug("Intervals.icu fetch is already running")

    def _build_candidate_settings(self) -> AppSettings:
        """Build a complete settings candidate from the current form values."""
        current = self.app.app_settings
        if (
            self.personal_section is None
            or self.display_section is None
            or self.location_section is None
            or self.pebble_section is None
            or self.provider_section is None
        ):
            message = "Settings sections have not been built"
            raise RuntimeError(message)
        values = current.model_dump(mode="python")
        values["personal"] = self.personal_section.settings_data()
        values["display"].update(self.display_section.settings_data())
        values["location"].update(self.location_section.settings_data())
        values["pebble"].update(self.pebble_section.settings_data())
        values.update(self.provider_section.settings_data())

        section_fields = {
            "running": "running_sensors",
            "cycling": "cycling_sensors",
            "trainer_running": "trainer_running",
            "trainer_cycling": "trainer_cycling",
        }
        for section_name, settings_field in section_fields.items():
            values[settings_field] = self.sensor_sections[section_name].settings_data()

        candidate = AppSettings.model_validate(values)
        candidate._settings_dir = current._settings_dir  # noqa: SLF001
        return candidate

    def _install_candidate_settings(self, candidate: AppSettings) -> None:
        """Install saved values into the already-built settings sections."""
        if self.personal_section is not None:
            self.personal_section.settings = candidate.personal
        if self.display_section is not None:
            self.display_section.settings = candidate.display
        if self.location_section is not None:
            self.location_section.settings = candidate.location
        if self.pebble_section is not None:
            self.pebble_section.set_settings(candidate.pebble)
        if self.provider_section is not None:
            self.provider_section.set_settings(candidate.icu, candidate.database)

    def _on_save_settings(self, _button: Gtk.Button) -> None:
        try:
            candidate = self._build_candidate_settings()
        except ValueError as exc:
            self.app.show_toast(f"Invalid settings: {exc}")
            return

        try:
            candidate.save()
        except OSError as exc:
            self.app.show_toast(f"Unable to save settings: {exc}")
            return

        self.app.app_settings = candidate
        section_fields = {
            "running": "running_sensors",
            "cycling": "cycling_sensors",
            "trainer_running": "trainer_running",
            "trainer_cycling": "trainer_cycling",
        }
        for section_name, settings_field in section_fields.items():
            self.sensor_sections[section_name].settings = getattr(candidate, settings_field)
        self._install_candidate_settings(candidate)

        self.app.refresh_hr_zones()
        self._update_actions_state()

        # Apply Pebble settings right away without restarting the application.
        _idle_once(self.app.apply_pebble_settings)

        # Keep the active session's recorder profile stable. The saved sensor
        # selections are picked up when the next session requests its profile.
        def apply_sensor_settings() -> bool:
            if self.app.apply_sensor_settings() is None:
                self.app.show_toast(
                    "Sensor settings saved; they will apply after the current session",
                )
            return False

        GLib.idle_add(apply_sensor_settings)

        toast = Adw.Toast.new("Settings saved successfully")
        GLib.idle_add(self.app.toast_overlay.add_toast, toast)

        _idle_once(self.app.tracker.redraw)
        _idle_once(self.app.tracker.refresh_units)
        _idle_once(self.app.history.refresh_units)

    def _on_sync(self, button: Gtk.Button) -> None:
        # disable the Settings-page sync button
        button.set_sensitive(False)
        self.app.show_toast("Syncing…")
        dsn = self.app.app_settings.database.dsn
        if not dsn:
            self.app.show_toast("No database DSN configured")
            button.set_sensitive(True)
            return
        database = self.app.database

        def work(_token: CancellationToken) -> None:
            database.sync_to_database(dsn)

        def on_success(_result: object) -> None:
            self.app.history.refresh()
            self.app.show_toast("Sync complete")

        def on_error(error: Exception) -> None:
            self.app.show_toast(f"Sync failed: {error}")

        try:
            self.app.jobs.submit(
                "database-sync",
                work,
                on_success=on_success,
                on_error=on_error,
                on_finally=lambda: button.set_sensitive(True),
            )
        except DuplicateJobError:
            logger.debug("Database sync is already running")
            button.set_sensitive(True)

    def _on_upload_success(self, results: list[tuple[int, bool, str | None]]) -> None:
        """Report the outcome of an Intervals.icu upload batch."""
        if not results:
            self.app.show_toast("No new activities to upload")
            return
        self.app.history.refresh()
        ok = sum(1 for _, succeeded, _ in results if succeeded)
        failures = [error for _, succeeded, error in results if not succeeded]
        if ok:
            self.app.show_toast(
                f"✅ Uploaded {ok} new {'activities' if ok > 1 else 'activity'}",
            )
        if failures:
            self.app.show_toast(f"⚠️ {len(failures)} failed")

    def _on_upload_error(self, error: Exception) -> None:
        """Show a user-facing upload error."""
        if isinstance(error, IntegrationError):
            self.app.show_toast(f"Intervals.icu error: {error}")
        else:
            self.app.show_toast(f"Upload failed: {error}")

    def _enable_upload_button(self) -> None:
        """Re-enable the upload action after the job exits."""
        if self.actions_section is not None and self.actions_section.upload_button is not None:
            self.actions_section.upload_button.set_sensitive(True)

    def _on_upload_icu(self, _button: Gtk.Button) -> None:
        repository = self.app.database.repository
        athlete_id = self.app.app_settings.icu.athlete_id
        api_key = self.app.app_settings.icu.api_key

        if self.actions_section is not None and self.actions_section.upload_button is not None:
            self.actions_section.upload_button.set_sensitive(False)
        self.app.show_toast("Uploading…")

        def work(_token: CancellationToken) -> list[tuple[int, bool, str | None]]:
            return upload_intervals_activities(
                athlete_id=athlete_id,
                api_key=api_key,
                repository=repository,
            )

        try:
            self.app.jobs.submit(
                "intervals-upload",
                work,
                on_success=self._on_upload_success,
                on_error=self._on_upload_error,
                on_finally=self._enable_upload_button,
            )
        except DuplicateJobError:
            logger.debug("Intervals.icu upload is already running")

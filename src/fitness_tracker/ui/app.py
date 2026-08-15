"""Application shell and UI composition."""

import contextlib
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import gi
from loguru import logger
from pebble_bridge import PebbleBridge
from xdg_base_dirs import (
    xdg_config_home,
    xdg_data_home,
)

from fitness_tracker.core.file_permissions import secure_directory
from fitness_tracker.core.sensor_profile import SensorProfile
from fitness_tracker.core.settings import AppSettings, SensorSettings, TrainerSettings
from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.core.units import UnitSystem
from fitness_tracker.core.zones import ChartTheme, HeartRateZones, ZoneThresholds
from fitness_tracker.database import DatabaseManager
from fitness_tracker.hardware.recorder import FinalizationStatus, Recorder
from fitness_tracker.services.jobs import (
    BackgroundJobRunner,
    CancellationToken,
    DuplicateJobError,
    JobRunnerShutdownError,
)
from fitness_tracker.ui.pages.history import HistoryPageUI
from fitness_tracker.ui.pages.settings import SettingsPageUI
from fitness_tracker.ui.pages.tracker import TrackerPageUI

gi.require_versions({"Gtk": "4.0", "Adw": "1"})

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402  # ty:ignore[unresolved-import]


@dataclass(frozen=True)
class _SensorProfileRequest:
    generation: int
    sport_type: SportTypesEnum
    trainer: bool
    profile: SensorProfile
    on_ready: Callable[[], None] | None = None


@dataclass(frozen=True)
class _SensorApplyResult:
    request: _SensorProfileRequest
    replacement: Recorder | None


@dataclass(frozen=True)
class _FinalizationWaitResult:
    """Describe whether a finalization waiter was accepted or deduplicated."""

    waiting: bool
    registered: bool


@dataclass(frozen=True)
class _FinalizationWaiter:
    """Associate one finalization callback with its owning UI object."""

    owner: object | None
    callback: Callable[[int | None], None]


class _SensorProfileApplyError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        request: _SensorProfileRequest,
        current: Recorder | None,
        clear_current: bool,
    ) -> None:
        self.request = request
        self.current = current
        self.clear_current = clear_current
        super().__init__(message)


_SENSOR_SOURCES: dict[SportTypesEnum, Callable[[AppSettings], SensorSettings]] = {
    SportTypesEnum.running: lambda settings: settings.running_sensors,
    SportTypesEnum.biking: lambda settings: settings.cycling_sensors,
}
_TRAINER_SOURCES: dict[SportTypesEnum, Callable[[AppSettings], TrainerSettings]] = {
    SportTypesEnum.running: lambda settings: settings.trainer_running,
    SportTypesEnum.biking: lambda settings: settings.trainer_cycling,
}


class FitnessAppUI(Adw.Application):
    """Compose the application window, pages, recorder, and background services."""

    def __init__(self, *, test_mode: bool = False) -> None:
        Adw.init()
        super().__init__(application_id="com.luigi311.fitness-tracker")
        self.connect("shutdown", self._on_shutdown)
        self.test_mode = test_mode
        style_manager = Adw.StyleManager.get_default()
        self.chart_theme = ChartTheme.for_style(style_manager.get_dark())
        style_manager.connect("notify::dark", self._on_style_changed)

        self.window = None
        self.recorder: Recorder | None = None
        self._pending_finalizations: list[Recorder] = []
        self._finalization_waiters: dict[
            int,
            tuple[Recorder, list[_FinalizationWaiter]],
        ] = {}
        self._sensor_apply_lock = threading.Lock()
        self._sensor_state_lock = threading.Lock()
        self._sensor_request: _SensorProfileRequest | None = None
        self._sensor_retiring: Recorder | None = None
        self._sensor_generation = 0
        self._sensor_apply_running = False
        self.jobs = BackgroundJobRunner(GLib.idle_add)

        self.history_filter = "week"

        # Set up application directory
        data_dir = Path(xdg_data_home()) / "fitness_tracker"
        config_dir = Path(xdg_config_home()) / "fitness_tracker"
        secure_directory(data_dir)
        secure_directory(config_dir)

        self.database_path = data_dir / "fitness.db"
        self.database = DatabaseManager(f"sqlite:///{self.database_path}")
        self.workouts_running_dir = data_dir / "workouts" / "running"
        secure_directory(self.workouts_running_dir)
        self.workouts_cycling_dir = data_dir / "workouts" / "cycling"
        secure_directory(self.workouts_cycling_dir)

        self.pebble_bridge = None

        # Load settings from config file
        self._settings_error: str | None = None
        try:
            self.app_settings: AppSettings = AppSettings.load(config_dir, create_if_missing=True)
            self._settings_error = self.app_settings.recovery_message
            if self._settings_error:
                logger.error(self._settings_error)
        except ValueError as exc:
            self.app_settings = AppSettings.recover(config_dir)
            self._settings_error = f"Invalid settings; defaults loaded: {exc}"
            logger.error(self._settings_error)
        self.refresh_hr_zones()

    @property
    def unit_system(self) -> UnitSystem:
        """Return the currently selected display unit system."""
        return self.app_settings.display.unit_system

    def _on_style_changed(
        self,
        style_manager: "Adw.StyleManager",
        _param_spec: object,
    ) -> None:
        """Refresh chart palettes when the desktop style changes at runtime."""
        self.chart_theme = ChartTheme.for_style(style_manager.get_dark())
        tracker = self.__dict__.get("tracker")
        if tracker is not None:
            tracker.refresh_theme()
        history = self.__dict__.get("history")
        if history is not None:
            history.refresh_theme()

    def _on_shutdown(self, _app: "Adw.Application") -> None:
        """Stop background services and active recorders during shutdown."""
        logger.debug("shutdown signal fired")
        self.jobs.shutdown()
        with self._sensor_apply_lock:
            with self._sensor_state_lock:
                recorders_by_identity: dict[int, Recorder] = {}
                for recorder in (
                    self.recorder,
                    self._sensor_retiring,
                    *self._pending_finalizations,
                ):
                    if recorder is not None:
                        recorders_by_identity[id(recorder)] = recorder
                recorders = tuple(recorders_by_identity.values())
                self.recorder = None
                self._sensor_retiring = None
                self._sensor_request = None
                self._pending_finalizations.clear()
                self._finalization_waiters.clear()
            for recorder in recorders:
                try:
                    recorder.shutdown()
                finally:
                    self._reconcile_shutdown_finalization(recorder)
        if self.pebble_bridge:
            with contextlib.suppress(Exception):
                self.pebble_bridge.stop(wait=False)

    def _on_close_request(self, *_a: object) -> bool:
        """Request application termination when the main window closes."""
        logger.debug("close-request fired")
        self.quit()
        return False

    def show_toast(self, message: str) -> None:
        """Display a user-facing message unless it is a routine BLE miss."""
        if "BleakDeviceNotFoundError" in message:
            logger.debug(message)
            return
        logger.info(message)
        # Create and display a toast on our overlay
        toast = Adw.Toast.new(message)
        self.toast_overlay.add_toast(toast)

    def show_finalization_pending(self, recorder: Recorder) -> None:
        """Show an actionable toast for an activity awaiting finalization."""
        with self._sensor_state_lock:
            if recorder not in self._pending_finalizations:
                self._pending_finalizations.append(recorder)

        toast = Adw.Toast.new("Activity finalization is pending")
        toast.set_button_label("Retry")

        def retry(_toast: Adw.Toast) -> None:
            self._retry_pending_finalization(recorder)

        toast.connect("button-clicked", retry)
        self.toast_overlay.add_toast(toast)

    def schedule_finalization(self, recorder: Recorder) -> None:
        """Finalize a recorder in a background job and update history on success."""
        claim = recorder.begin_finalization()
        if claim.status is FinalizationStatus.ALREADY_RUNNING:
            self.show_toast("Activity finalization is already running")
            return
        if claim.status is not FinalizationStatus.STARTED or claim.activity_id is None:
            return

        activity_id = claim.activity_id
        job_name = f"finalize-activity-{activity_id}"

        def work(_token: CancellationToken) -> int | None:
            return recorder.finish_finalization(activity_id)

        try:
            self.jobs.submit(
                job_name,
                work,
                on_success=lambda finalized_id: self._on_finalization_result(
                    recorder,
                    finalized_id,
                ),
                on_error=lambda _error: self._on_finalization_result(recorder, None),
                on_finally=lambda: self._on_finalization_delivery_finally(recorder),
                on_discard=lambda: self._discard_finalization_waiters(recorder),
            )
        except (DuplicateJobError, JobRunnerShutdownError):
            recorder.abort_finalization(activity_id)
            if recorder.finalization_pending:
                self.show_finalization_pending(recorder)

    def _on_finalization_result(
        self,
        recorder: Recorder,
        activity_id: int | None,
    ) -> None:
        if activity_id is None:
            if recorder.finalization_pending:
                self.show_finalization_pending(recorder)
            self._notify_finalization_waiters(recorder, None)
            return

        if recorder.claim_finalization_reconciliation(activity_id):
            self._discard_pending_finalization(recorder)
            GLib.idle_add(self.history.append_activity, activity_id)
            self.show_toast("Activity finalization completed")
        self._notify_finalization_waiters(recorder, activity_id)

    def wait_for_finalization(
        self,
        recorder: Recorder,
        on_complete: Callable[[int | None], None],
        *,
        owner: object | None = None,
    ) -> _FinalizationWaitResult:
        """Register a callback for a recorder's pending or active finalization."""
        with self._sensor_state_lock:
            if not (recorder.finalization_in_progress or recorder.finalization_pending):
                return _FinalizationWaitResult(waiting=False, registered=False)

            key = id(recorder)
            entry = self._finalization_waiters.get(key)
            if entry is None:
                waiters: list[_FinalizationWaiter] = []
                self._finalization_waiters[key] = (recorder, waiters)
            else:
                registered_recorder, waiters = entry
                if registered_recorder is not recorder:
                    waiters = []
                    self._finalization_waiters[key] = (recorder, waiters)

            if owner is not None and any(waiter.owner is owner for waiter in waiters):
                return _FinalizationWaitResult(waiting=True, registered=False)

            waiters.append(_FinalizationWaiter(owner=owner, callback=on_complete))
        return _FinalizationWaitResult(waiting=True, registered=True)

    def _notify_finalization_waiters(
        self,
        recorder: Recorder,
        activity_id: int | None,
    ) -> bool:
        """Release and invoke waiters, returning whether any were registered."""
        with self._sensor_state_lock:
            entry = self._finalization_waiters.pop(id(recorder), None)
        if entry is None or entry[0] is not recorder:
            return False
        for waiter in entry[1]:
            try:
                waiter.callback(activity_id)
            except Exception:
                logger.exception("Deferred finalization callback failed")
        return bool(entry[1])

    def _discard_finalization_waiters(self, recorder: Recorder) -> None:
        """Clear waiters when the job cannot marshal any UI callback."""
        with self._sensor_state_lock:
            key = id(recorder)
            entry = self._finalization_waiters.get(key)
            if entry is not None and entry[0] is recorder:
                self._finalization_waiters.pop(key, None)

    def _on_finalization_delivery_finally(self, recorder: Recorder) -> None:
        """Recover waiters if a job ended without a success/error callback."""
        if self._notify_finalization_waiters(recorder, None) and recorder.finalization_pending:
            self.show_finalization_pending(recorder)

    def _retry_pending_finalization(self, recorder: Recorder) -> None:
        with self._sensor_state_lock:
            is_pending = recorder in self._pending_finalizations
        if not is_pending or not recorder.finalization_pending:
            self._discard_pending_finalization(recorder)
            self.show_toast("No activity finalization is pending")
            return

        self.schedule_finalization(recorder)

    def _discard_pending_finalization(self, recorder: Recorder) -> None:
        with self._sensor_state_lock:
            self._pending_finalizations = [
                pending for pending in self._pending_finalizations if pending is not recorder
            ]

    def show_workout_step_notification(
        self,
        step_number: int,
        step_count: int,
        target_text: str,
        *,
        announce: bool = True,
    ) -> None:
        """Update step integrations and optionally announce a workout step change."""
        if self.pebble_bridge:
            self.pebble_bridge.update(workout_step=step_number - 1)
        if not announce:
            return

        title = f"Workout step {step_number} of {step_count}"
        self.show_toast(f"{title}: {target_text}")
        self._send_background_notification(
            "workout-step-change",
            title,
            target_text,
        )

    def show_workout_complete_notification(self) -> None:
        """Announce completion in-app and, when unfocused, on the desktop."""
        message = "✅ Workout complete. Continuing in Free Run…"
        self.show_toast(message)
        self._send_background_notification(
            "workout-complete",
            "Workout complete",
            "Continuing in Free Run",
        )

    def _send_background_notification(
        self,
        notification_id: str,
        title: str,
        body: str,
    ) -> None:
        """Send a desktop notification only when the main window is not active."""
        if self.window is not None and self.window.is_active():
            return

        notification = Gio.Notification.new(title)
        notification.set_body(body)
        self.send_notification(notification_id, notification)

    def apply_pebble_settings(self) -> None:
        """Configure, update, or stop the Pebble bridge for current settings."""
        tracker = self.__dict__.get("tracker")
        recording_active = tracker is not None and tracker.recording_active

        if self.pebble_bridge:
            # Skip teardown and recreation if no settings change
            if (
                self.app_settings.pebble.address == self.pebble_bridge.mac
                and self.app_settings.pebble.use_emulator == self.pebble_bridge.use_emulator
                and self.app_settings.pebble.port == self.pebble_bridge.port
            ):
                self.pebble_bridge.update(
                    units=int(self.unit_system == UnitSystem.IMPERIAL),
                )
                return

            with contextlib.suppress(Exception):
                self.pebble_bridge.stop(wait=False)
        self.pebble_bridge = None

        if not self.app_settings.pebble.enable:
            logger.debug("Pebble Disabled")
            return

        try:
            if not self.app_settings.pebble.use_emulator and not hasattr(socket, "AF_BLUETOOTH"):
                # Check if python sock has AF_BLUETOOTH support
                # Do not attempt to start the bridge if no Bluetooth support
                # Clear out connection info
                self.app_settings.pebble.address = None

                msg = "No Bluetooth support in Python socket module"
                logger.error(msg)
                self.show_toast(msg)
                return

            self.pebble_bridge = PebbleBridge(
                app_uuid=self.app_settings.pebble.uuid,
                mac=self.app_settings.pebble.address,
                use_emulator=self.app_settings.pebble.use_emulator,
                port=self.app_settings.pebble.port,
            )

            self.pebble_bridge.update(
                units=int(self.unit_system == UnitSystem.IMPERIAL),
            )
            if recording_active:
                self.pebble_bridge.start()
        except Exception as e:
            self.pebble_bridge = None
            logger.error(e)

    def _profile_from_sport_type(
        self,
        sport_type: SportTypesEnum,
        *,
        trainer: bool = False,
    ) -> SensorProfile:
        """Convert a SportTypesEnum to a SensorProfile object."""
        if trainer:
            source = _TRAINER_SOURCES.get(sport_type)
            if source is not None:
                return SensorProfile.from_trainer_settings(source(self.app_settings))
        else:
            source = _SENSOR_SOURCES.get(sport_type)
            if source is not None:
                return SensorProfile.from_sensor_settings(source(self.app_settings))

        suffix = " for trainer" if trainer else ""
        logger.error(
            "Unknown profile '{}'{}. Defaulting to empty profile.",
            sport_type,
            suffix,
        )
        return SensorProfile()

    def apply_sensor_settings(
        self,
        sport_type: SportTypesEnum = SportTypesEnum.running,
        *,
        trainer: bool = False,
        on_ready: Callable[[], None] | None = None,
        allow_during_session: bool = False,
    ) -> int | None:
        """Install the latest requested recorder profile asynchronously."""
        desired = self._profile_from_sport_type(sport_type, trainer=trainer)
        tracker = self.__dict__.get("tracker")
        if tracker is not None and tracker.session_open and not allow_during_session:
            logger.info("Deferring sensor profile changes until the current session ends")
            return None

        with self._sensor_state_lock:
            self._sensor_generation += 1
            request = _SensorProfileRequest(
                generation=self._sensor_generation,
                sport_type=sport_type,
                trainer=trainer,
                profile=desired,
                on_ready=on_ready,
            )
            self._sensor_request = request
            current = self.recorder
            if current is not None and not self._recorder_matches(current, request):
                self.recorder = None
                if self._sensor_retiring is None:
                    self._sensor_retiring = current

            already_ready = (
                current is not None
                and self._sensor_retiring is None
                and self._recorder_matches(current, request)
            )
            start_worker = not already_ready and not self._sensor_apply_running
            if start_worker:
                self._sensor_apply_running = True

        if already_ready:
            self._schedule_profile_ready(request)
        elif start_worker:
            self._submit_sensor_apply()
        return request.generation

    @staticmethod
    def _recorder_matches(recorder: Recorder, request: _SensorProfileRequest) -> bool:
        return recorder.sport_type == request.sport_type and recorder.profile == request.profile

    def _schedule_profile_ready(self, request: _SensorProfileRequest) -> None:
        def deliver() -> bool:
            with self._sensor_state_lock:
                ready = (
                    self._sensor_request is request
                    and self.recorder is not None
                    and self._recorder_matches(self.recorder, request)
                )
            if ready and request.on_ready is not None:
                request.on_ready()
            return False

        GLib.idle_add(deliver)

    def _submit_sensor_apply(self) -> None:
        try:
            self.jobs.submit(
                "sensor-profile-apply",
                self._run_sensor_apply,
                on_success=self._on_sensor_apply_success,
                on_error=self._on_sensor_apply_error,
            )
        except DuplicateJobError:
            logger.debug("Sensor profile apply is already running")

    def _run_sensor_apply(self, token: CancellationToken) -> _SensorApplyResult:
        with self._sensor_apply_lock:
            with self._sensor_state_lock:
                request = self._sensor_request
                current = self._sensor_retiring
                self._sensor_retiring = None
            if request is None:
                message = "No sensor profile request is available"
                raise RuntimeError(message)

            if current is not None:
                try:
                    shutdown_ok = False
                    try:
                        shutdown_ok = current.shutdown()
                    finally:
                        self._reconcile_shutdown_finalization(current)
                except Exception as error:
                    message = f"Unable to stop the current sensor worker: {error}"
                    raise _SensorProfileApplyError(
                        message,
                        request=request,
                        current=current,
                        clear_current=False,
                    ) from error
                if not shutdown_ok:
                    message = (
                        "The current sensor worker is still shutting down; profile unavailable"
                    )
                    raise _SensorProfileApplyError(
                        message,
                        request=request,
                        current=current,
                        clear_current=False,
                    )

            token.raise_if_cancelled()
            with self._sensor_state_lock:
                request = self._sensor_request
            if request is None:
                message = "No sensor profile request is available"
                raise RuntimeError(message)

            try:
                logger.debug(
                    f"Applying sensor settings for profile '{request.sport_type}': "
                    f"{request.profile}",
                )
                replacement = Recorder(
                    profile=request.profile,
                    weight_kg=self.app_settings.personal.weight_kg,
                    sport_type=request.sport_type,
                    on_sample_update=self.tracker.on_sample,
                    database=self.database,
                    on_error=self.show_toast,
                    test_mode=self.test_mode,
                    dispatch=GLib.idle_add,
                )
            except Exception as error:
                message = f"Unable to build the sensor worker: {error}"
                raise _SensorProfileApplyError(
                    message,
                    request=request,
                    current=current,
                    clear_current=current is not None,
                ) from error

            return _SensorApplyResult(request=request, replacement=replacement)

    def _reconcile_shutdown_finalization(self, recorder: Recorder) -> None:
        activity_id = recorder.take_shutdown_finalized_activity_id()
        if activity_id is None:
            return
        if not recorder.claim_finalization_reconciliation(activity_id):
            return
        self._discard_pending_finalization(recorder)
        GLib.idle_add(self.history.append_activity, activity_id)

    def _on_sensor_apply_success(self, result: _SensorApplyResult) -> None:
        replacement = result.replacement
        if replacement is None:
            return

        with self._sensor_state_lock:
            latest = self._sensor_request
            stale = latest is None or latest.generation != result.request.generation

        if stale:
            with contextlib.suppress(Exception):
                replacement.shutdown()
            self._submit_sensor_apply()
            return

        try:
            if not self.test_mode:
                replacement.start()
        except Exception as error:
            with contextlib.suppress(Exception):
                replacement.shutdown()
            with self._sensor_state_lock:
                latest = self._sensor_request
                current_request = latest is result.request
                if current_request:
                    self._sensor_apply_running = False
            if current_request:
                self.show_toast(f"Unable to start the sensor worker: {error}")
            else:
                self._submit_sensor_apply()
            return

        with self._sensor_state_lock:
            latest = self._sensor_request
            stale = latest is None or latest.generation != result.request.generation
            if not stale:
                self.recorder = replacement
                self._sensor_apply_running = False

        if stale:
            with contextlib.suppress(Exception):
                replacement.shutdown()
            self._submit_sensor_apply()
            return

        self.tracker.update_metric_statuses()
        if result.request.on_ready is not None:
            result.request.on_ready()

    def _on_sensor_apply_error(self, error: Exception) -> None:
        with self._sensor_state_lock:
            self._sensor_apply_running = False
            if isinstance(error, _SensorProfileApplyError) and error.current is not None:
                self._sensor_retiring = error.current
            latest = self._sensor_request
            retry_latest = (
                isinstance(error, _SensorProfileApplyError)
                and latest is not None
                and latest.generation > error.request.generation
            )
            if retry_latest:
                self._sensor_apply_running = True
        if isinstance(error, _SensorProfileApplyError):
            self.show_toast(str(error))
        else:
            self.show_toast(f"Unable to apply sensor settings: {error}")
        if retry_latest:
            self._submit_sensor_apply()

    def do_activate(self) -> None:
        """Create and present the main window when the application activates."""
        if not self.window:
            self._build_ui()

            if self._settings_error:
                self.show_toast(self._settings_error)
                self._settings_error = None

            # Start/stop Pebble according to config
            self.apply_pebble_settings()

            # Start/stop recorder with sensors
            self.apply_sensor_settings(sport_type=SportTypesEnum.running, trainer=False)

        self.window.present()

    def _build_ui(self) -> None:
        prov = Gtk.CssProvider()
        prov.load_from_data(b"""
        .pill { padding: 4px 10px; border-radius: 9999px; color: white; }
        .pill-in   { background-color: rgba(51,204,77,0.95); }   /* #33CC4D-ish */
        .pill-near { background-color: rgba(242,191,51,0.95); }  /* amber */
        .pill-low  { background-color: rgba(242,140,51,0.95); }  /* orange */
        .pill-high { background-color: rgba(242,89,89,0.95); }   /* red */
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            prov,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self.window = Adw.ApplicationWindow(application=self)
        self.window.connect("close-request", self._on_close_request)
        self.window.set_title("Fitness Tracker")
        self.window.set_default_size(720, 1280)
        self.window.set_resizable(True)
        self.toast_overlay = Adw.ToastOverlay()
        self.window.set_content(self.toast_overlay)

        toolbar_view = Adw.ToolbarView()
        self.toast_overlay.set_child(toolbar_view)

        # Add header bar to the top of toolbar
        header_bar = Adw.HeaderBar()
        header_bar.set_show_title(True)

        self.header_revealer = Gtk.Revealer()
        self.header_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.header_revealer.set_reveal_child(False)  # default to hidden (mobile)

        self.header_revealer.set_child(header_bar)
        toolbar_view.add_top_bar(self.header_revealer)

        # Create ViewStack
        self.stack = Adw.ViewStack()
        self.stack.set_vexpand(True)

        self.tracker = TrackerPageUI(self)
        self.history = HistoryPageUI(self)
        self.settings = SettingsPageUI(self)

        tracker = self.tracker.build_page()
        history = self.history.build_page()
        settings = self.settings.build_page()

        self.stack.add_titled(tracker, "tracker", "Tracker").set_icon_name(
            "media-playback-start-symbolic",
        )
        self.stack.add_titled(history, "history", "History").set_icon_name("view-list-symbolic")
        self.stack.add_titled(settings, "settings", "Settings").set_icon_name(
            "emblem-system-symbolic",
        )

        switcher_bar = Adw.ViewSwitcherBar()
        switcher_bar.set_stack(self.stack)
        switcher_bar.set_reveal(True)

        toolbar_view.set_content(self.stack)
        toolbar_view.add_bottom_bar(switcher_bar)

        cond = Adw.BreakpointCondition.parse("min-width: 700sp")
        bp = Adw.Breakpoint.new(cond)
        bp.add_setter(self.header_revealer, "reveal-child", value=True)
        self.window.add_breakpoint(bp)

    def refresh_hr_zones(self) -> None:
        """Rebuild cached heart-rate zones after personal settings change."""
        self.hr_zones = HeartRateZones(
            resting_hr=self.app_settings.personal.resting_hr,
            max_hr=self.app_settings.personal.max_hr,
        )

    def calculate_hr_zones(self) -> ZoneThresholds:
        """Return the cached Karvonen heart-rate thresholds."""
        return self.hr_zones.thresholds

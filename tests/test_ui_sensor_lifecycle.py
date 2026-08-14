# ruff: noqa: E402, PT009, SLF001

import importlib
import queue
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import gi
import gi.repository

_MISSING = object()
_GI_NAMES = ("Adw", "Gdk", "Gio", "GLib", "Gtk")
_STUB_MODULE_NAMES = (
    "pebble_bridge",
    "fitness_tracker.ui.pages.history",
    "fitness_tracker.ui.pages.settings",
    "fitness_tracker.ui.pages.tracker",
)
_original_require_versions = gi.require_versions
gi.require_versions = lambda _versions: None
_original_gi_attributes = {name: getattr(gi.repository, name, _MISSING) for name in _GI_NAMES}
_original_stub_modules = {name: sys.modules.get(name, _MISSING) for name in _STUB_MODULE_NAMES}


class _Application:
    pass


class _GLib:
    callbacks: queue.Queue = queue.Queue()

    @classmethod
    def idle_add(cls, callback, *args):
        cls.callbacks.put(lambda: callback(*args))
        return 1


class _Notification:
    def __init__(self, title):
        self.title = title
        self.body = None

    @classmethod
    def new(cls, title):
        return cls(title)

    def set_body(self, body):
        self.body = body


class _Toast:
    def __init__(self, message):
        self.message = message
        self.button_label = None
        self._button_callback = None

    @classmethod
    def new(cls, message):
        return cls(message)

    def set_button_label(self, label):
        self.button_label = label

    def connect(self, _signal, callback):
        self._button_callback = callback

    def click(self):
        assert self._button_callback is not None
        self._button_callback(self)


class _ToastOverlay:
    def __init__(self):
        self.toasts = []

    def add_toast(self, toast):
        self.toasts.append(toast)


gi.repository.Adw = types.SimpleNamespace(Application=_Application, Toast=_Toast)
gi.repository.Gdk = types.SimpleNamespace()
gi.repository.Gio = types.SimpleNamespace(Notification=_Notification)
gi.repository.GLib = _GLib
gi.repository.Gtk = types.SimpleNamespace()

pebble_bridge = types.ModuleType("pebble_bridge")
pebble_bridge.PebbleBridge = object
sys.modules["pebble_bridge"] = pebble_bridge

ui_history = types.ModuleType("fitness_tracker.ui.pages.history")
ui_history.HistoryPageUI = object
sys.modules[ui_history.__name__] = ui_history

ui_settings = types.ModuleType("fitness_tracker.ui.pages.settings")
ui_settings.SettingsPageUI = object
sys.modules[ui_settings.__name__] = ui_settings

ui_tracker = types.ModuleType("fitness_tracker.ui.pages.tracker")
ui_tracker.TrackerPageUI = object
sys.modules[ui_tracker.__name__] = ui_tracker

try:
    importlib.import_module("fitness_tracker.ui.app")
finally:
    gi.require_versions = _original_require_versions
    for name, original in _original_gi_attributes.items():
        if original is _MISSING:
            delattr(gi.repository, name)
        else:
            setattr(gi.repository, name, original)
    for name, original in _original_stub_modules.items():
        if original is _MISSING:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original
from fitness_tracker.core.sensor_profile import SensorProfile
from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.hardware.recorder import FinalizationClaim, FinalizationStatus
from fitness_tracker.services.jobs import BackgroundJobRunner
from fitness_tracker.ui.app import (
    FitnessAppUI,
    _SensorProfileApplyError,
    _SensorProfileRequest,
)


class _CurrentRecorder:
    def __init__(self, *, result=True):
        self.result = result
        self.shutdown_started = threading.Event()
        self.shutdown_release = threading.Event()
        self.sport_type = SportTypesEnum.running
        self.profile = SensorProfile(hr_address="old")
        self.hr_address = "old"
        self.speed_address = ""
        self.cadence_address = ""
        self.power_address = ""
        self.trainer_address = ""
        self.trainer_machine_type = None
        self.finalized_activity_id = None

    def shutdown(self):
        self.shutdown_started.set()
        self.shutdown_release.wait(timeout=2)
        return self.result

    def take_shutdown_finalized_activity_id(self):
        activity_id = self.finalized_activity_id
        self.finalized_activity_id = None
        return activity_id

    def claim_finalization_reconciliation(self, _activity_id):
        return True


class SensorSettingsLifecycleTests(unittest.TestCase):
    def setUp(self):
        while not _GLib.callbacks.empty():
            _GLib.callbacks.get_nowait()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def _make_app(self):
        app = FitnessAppUI.__new__(FitnessAppUI)
        app._sensor_apply_lock = threading.Lock()
        app._sensor_state_lock = threading.Lock()
        app._sensor_request = None
        app._sensor_retiring = None
        app._pending_finalizations = []
        app._finalization_waiters = {}
        app._sensor_generation = 0
        app._sensor_apply_running = False
        app.jobs = BackgroundJobRunner(_GLib.idle_add)
        app.app_settings = types.SimpleNamespace(
            personal=types.SimpleNamespace(weight_kg=75.0),
        )
        app.database = Path(self.temp_dir.name) / "fitness-tracker-test.db"
        app.test_mode = True
        app.toasts = []
        app.show_toast = app.toasts.append
        app.profile_installed = threading.Event()
        app.tracker = types.SimpleNamespace(
            on_sample=lambda _sample: None,
            update_metric_statuses=app.profile_installed.set,
            session_open=False,
        )
        app._profile_from_sport_type = lambda _sport_type, trainer=False: SensorProfile(  # noqa: ARG005 - match keyword callback signature
            hr_address="new",
        )
        return app

    def _run_idle(self):
        _GLib.callbacks.get(timeout=2)()

    def test_workout_step_notification_uses_toast_and_desktop_notification(self):
        app = FitnessAppUI.__new__(FitnessAppUI)
        toasts = []
        notifications = []
        pebble_steps = []
        app.show_toast = toasts.append
        app.send_notification = lambda notification_id, notification: notifications.append(
            (notification_id, notification),
        )
        app.pebble_bridge = types.SimpleNamespace(
            update=lambda **values: pebble_steps.append(values["workout_step"]),
        )

        app.show_workout_step_notification(2, 5, "Target: 150 - 175 W")

        self.assertEqual(pebble_steps, [1])
        self.assertEqual(toasts, ["Workout step 2 of 5: Target: 150 - 175 W"])
        self.assertEqual(notifications[0][0], "workout-step-change")
        self.assertEqual(notifications[0][1].title, "Workout step 2 of 5")
        self.assertEqual(notifications[0][1].body, "Target: 150 - 175 W")

    def test_workout_step_preview_only_updates_pebble(self):
        app = FitnessAppUI.__new__(FitnessAppUI)
        pebble_steps = []
        app.pebble_bridge = types.SimpleNamespace(
            update=lambda **values: pebble_steps.append(values["workout_step"]),
        )
        app.show_toast = lambda _message: self.fail("Preview should not show a toast")
        app.send_notification = lambda *_args: self.fail(
            "Preview should not send a desktop notification",
        )

        app.show_workout_step_notification(1, 5, "Target: 100 W", announce=False)

        self.assertEqual(pebble_steps, [0])

    def test_changed_pebble_settings_restart_bridge_for_active_recording(self):
        app = FitnessAppUI.__new__(FitnessAppUI)
        old_bridge = Mock(mac="old", use_emulator=True, port=1234)
        replacement = Mock()
        app.pebble_bridge = old_bridge
        app.tracker = types.SimpleNamespace(recording_active=True)
        app.app_settings = types.SimpleNamespace(
            display=types.SimpleNamespace(unit_system=None),
            pebble=types.SimpleNamespace(
                address="new",
                enable=True,
                port=5678,
                use_emulator=True,
                uuid="test-uuid",
            ),
        )

        with patch("fitness_tracker.ui.app.PebbleBridge", return_value=replacement):
            app.apply_pebble_settings()

        old_bridge.stop.assert_called_once_with(wait=False)
        replacement.update.assert_called_once_with(units=0)
        replacement.start.assert_called_once_with()
        self.assertIs(app.pebble_bridge, replacement)

    def test_changed_pebble_settings_keep_bridge_stopped_when_recording_inactive(self):
        app = FitnessAppUI.__new__(FitnessAppUI)
        old_bridge = Mock(mac="old", use_emulator=True, port=1234)
        replacement = Mock()
        app.pebble_bridge = old_bridge
        app.tracker = types.SimpleNamespace(recording_active=False)
        app.app_settings = types.SimpleNamespace(
            display=types.SimpleNamespace(unit_system=None),
            pebble=types.SimpleNamespace(
                address="new",
                enable=True,
                port=5678,
                use_emulator=True,
                uuid="test-uuid",
            ),
        )

        with patch("fitness_tracker.ui.app.PebbleBridge", return_value=replacement):
            app.apply_pebble_settings()

        old_bridge.stop.assert_called_once_with(wait=False)
        replacement.update.assert_called_once_with(units=0)
        replacement.start.assert_not_called()
        self.assertIs(app.pebble_bridge, replacement)

    def test_workout_complete_uses_toast_and_desktop_notification(self):
        app = FitnessAppUI.__new__(FitnessAppUI)
        toasts = []
        notifications = []
        app.show_toast = toasts.append
        app.send_notification = lambda notification_id, notification: notifications.append(
            (notification_id, notification),
        )

        app.show_workout_complete_notification()

        self.assertEqual(toasts, ["✅ Workout complete. Continuing in Free Run…"])
        self.assertEqual(notifications[0][0], "workout-complete")
        self.assertEqual(notifications[0][1].title, "Workout complete")
        self.assertEqual(notifications[0][1].body, "Continuing in Free Run")

    def test_pending_finalization_toast_retries_and_refreshes_history(self):
        app = FitnessAppUI.__new__(FitnessAppUI)
        app._sensor_state_lock = threading.Lock()
        app._pending_finalizations = []
        app.toast_overlay = _ToastOverlay()
        app.history = types.SimpleNamespace(append_activity=Mock())
        app.jobs = BackgroundJobRunner(_GLib.idle_add)
        app._finalization_waiters = {}
        app.show_toast = lambda message: app.toast_overlay.add_toast(_Toast(message))
        app.recorder = types.SimpleNamespace(
            finalization_pending=True,
            finalization_in_progress=False,
            claim_finalization_reconciliation=Mock(return_value=True),
            begin_finalization=Mock(
                return_value=FinalizationClaim(FinalizationStatus.STARTED, 17),
            ),
            finish_finalization=Mock(return_value=17),
            abort_finalization=Mock(),
        )

        app.show_finalization_pending(app.recorder)

        toast = app.toast_overlay.toasts[0]
        self.assertEqual(toast.message, "Activity finalization is pending")
        self.assertEqual(toast.button_label, "Retry")
        toast.click()
        _GLib.callbacks.get(timeout=2)()
        _GLib.callbacks.get(timeout=2)()

        app.recorder.begin_finalization.assert_called_once_with()
        app.recorder.finish_finalization.assert_called_once_with(17)
        app.history.append_activity.assert_called_once_with(17)
        self.assertEqual(
            [toast.message for toast in app.toast_overlay.toasts],
            ["Activity finalization is pending", "Activity finalization completed"],
        )

    def test_pending_finalization_retry_survives_recorder_replacement(self):
        app = FitnessAppUI.__new__(FitnessAppUI)
        app._sensor_state_lock = threading.Lock()
        app._pending_finalizations = []
        app.toast_overlay = _ToastOverlay()
        app.history = types.SimpleNamespace(append_activity=Mock())
        app.jobs = BackgroundJobRunner(_GLib.idle_add)
        app._finalization_waiters = {}
        app.show_toast = lambda message: app.toast_overlay.add_toast(_Toast(message))
        pending = types.SimpleNamespace(
            finalization_pending=True,
            finalization_in_progress=False,
            claim_finalization_reconciliation=Mock(return_value=True),
            begin_finalization=Mock(
                return_value=FinalizationClaim(FinalizationStatus.STARTED, 23),
            ),
            finish_finalization=Mock(return_value=23),
            abort_finalization=Mock(),
        )
        replacement = types.SimpleNamespace(
            finalization_pending=False,
            stop_recording=Mock(),
        )
        app.recorder = pending

        app.show_finalization_pending(pending)
        app.recorder = replacement
        app.toast_overlay.toasts[0].click()
        _GLib.callbacks.get(timeout=2)()
        _GLib.callbacks.get(timeout=2)()

        pending.begin_finalization.assert_called_once_with()
        pending.finish_finalization.assert_called_once_with(23)
        replacement.stop_recording.assert_not_called()
        app.history.append_activity.assert_called_once_with(23)
        assert app._pending_finalizations == []

    def test_finalization_completion_notifies_deferred_start(self):
        app = FitnessAppUI.__new__(FitnessAppUI)
        app._sensor_state_lock = threading.Lock()
        app._pending_finalizations = []
        app._finalization_waiters = {}
        app.history = types.SimpleNamespace(append_activity=Mock())
        app.show_toast = Mock()

        class _Recorder:
            finalization_pending = True
            finalization_in_progress = True

            def claim_finalization_reconciliation(self, _activity_id):
                return True

        recorder = _Recorder()
        callback = Mock()
        assert app.wait_for_finalization(recorder, callback).registered
        recorder.finalization_pending = False
        recorder.finalization_in_progress = False

        app._on_finalization_result(recorder, 31)

        callback.assert_called_once_with(31)
        assert id(recorder) not in app._finalization_waiters

    def test_finalization_waiters_deduplicate_by_owner(self):
        app = FitnessAppUI.__new__(FitnessAppUI)
        app._sensor_state_lock = threading.Lock()
        app._finalization_waiters = {}
        recorder = types.SimpleNamespace(
            finalization_pending=True,
            finalization_in_progress=True,
        )
        first_owner = object()
        second_owner = object()
        first_callback = Mock()
        duplicate_callback = Mock()
        second_callback = Mock()

        first = app.wait_for_finalization(
            recorder,
            first_callback,
            owner=first_owner,
        )
        duplicate = app.wait_for_finalization(
            recorder,
            duplicate_callback,
            owner=first_owner,
        )
        second = app.wait_for_finalization(
            recorder,
            second_callback,
            owner=second_owner,
        )

        assert first.waiting
        assert first.registered
        assert duplicate.waiting
        assert not duplicate.registered
        assert second.waiting
        assert second.registered

        app._notify_finalization_waiters(recorder, 31)

        first_callback.assert_called_once_with(31)
        duplicate_callback.assert_not_called()
        second_callback.assert_called_once_with(31)

    def test_failed_finalization_notifies_and_clears_deferred_waiters(self):
        app = FitnessAppUI.__new__(FitnessAppUI)
        app._sensor_state_lock = threading.Lock()
        app._pending_finalizations = []
        app._finalization_waiters = {}
        app.toast_overlay = _ToastOverlay()
        app.show_toast = lambda message: app.toast_overlay.add_toast(_Toast(message))

        class _UnhashableRecorder:
            finalization_pending = True
            finalization_in_progress = False

        recorder = _UnhashableRecorder()
        callback = Mock()

        assert app.wait_for_finalization(recorder, callback).registered
        app._on_finalization_result(recorder, None)

        callback.assert_called_once_with(None)
        assert id(recorder) not in app._finalization_waiters
        assert app._pending_finalizations == [recorder]

    def test_app_shutdown_waits_for_profile_apply_lock(self):
        app = self._make_app()
        current = _CurrentRecorder()
        current.shutdown_release.set()
        app.recorder = current
        app.pebble_bridge = None
        app._sensor_apply_lock.acquire()

        shutdown_thread = threading.Thread(target=app._on_shutdown, args=(None,))
        shutdown_thread.start()
        self.assertFalse(current.shutdown_started.wait(timeout=0.05))

        app._sensor_apply_lock.release()
        shutdown_thread.join(timeout=1)

        self.assertFalse(shutdown_thread.is_alive())
        self.assertTrue(current.shutdown_started.is_set())
        self.assertIsNone(app.recorder)

    def test_shutdown_and_rebuild_do_not_block_caller(self):
        app = self._make_app()
        current = _CurrentRecorder()
        replacement = types.SimpleNamespace()
        app.recorder = current

        with patch("fitness_tracker.ui.app.Recorder", return_value=replacement):
            app.apply_sensor_settings()
            self.assertTrue(current.shutdown_started.wait(timeout=1))
            self.assertIsNone(app.recorder)

            current.shutdown_release.set()
            self._run_idle()

        self.assertTrue(app.profile_installed.wait(timeout=1))
        self.assertIs(app.recorder, replacement)

    def test_retired_recorder_finalization_refreshes_history(self):
        app = self._make_app()
        current = _CurrentRecorder()
        current.finalized_activity_id = 41
        app.recorder = current
        app._pending_finalizations = [current]
        app.history = types.SimpleNamespace(append_activity=Mock())
        replacement = types.SimpleNamespace()

        with patch("fitness_tracker.ui.app.Recorder", return_value=replacement):
            app.apply_sensor_settings(sport_type=SportTypesEnum.biking)
            self.assertTrue(current.shutdown_started.wait(timeout=1))
            current.shutdown_release.set()
            while not app.profile_installed.is_set():
                self._run_idle()

        app.history.append_activity.assert_called_once_with(41)
        self.assertEqual(app._pending_finalizations, [])
        self.assertIs(app.recorder, replacement)

    def test_latest_sensor_profile_request_wins_during_shutdown(self):
        app = self._make_app()
        current = _CurrentRecorder()
        app.recorder = current
        replacements = []

        def build_replacement(**kwargs):
            replacement = types.SimpleNamespace(
                profile=kwargs["profile"],
                sport_type=kwargs["sport_type"],
            )
            replacements.append(replacement)
            return replacement

        with patch("fitness_tracker.ui.app.Recorder", side_effect=build_replacement):
            app.apply_sensor_settings(sport_type=SportTypesEnum.biking)
            self.assertTrue(current.shutdown_started.wait(timeout=1))

            app.apply_sensor_settings(sport_type=SportTypesEnum.running)
            current.shutdown_release.set()
            self._run_idle()

        self.assertEqual(len(replacements), 1)
        self.assertEqual(replacements[0].sport_type, SportTypesEnum.running)
        self.assertIs(app.recorder, replacements[0])

    def test_sensor_profile_changes_are_deferred_during_a_session(self):
        app = self._make_app()
        current = _CurrentRecorder()
        app.recorder = current
        app.tracker.session_open = True

        self.assertIsNone(app.apply_sensor_settings())
        self.assertIs(app.recorder, current)
        self.assertFalse(current.shutdown_started.is_set())

    def test_shutdown_failure_is_reported_on_ui_thread(self):
        app = self._make_app()
        current = _CurrentRecorder(result=False)
        current.shutdown_release.set()
        app.recorder = current

        with patch("fitness_tracker.ui.app.Recorder") as recorder_factory:
            app.apply_sensor_settings()
            self.assertTrue(current.shutdown_started.wait(timeout=1))
            self._run_idle()

        self.assertIsNone(app.recorder)
        self.assertEqual(
            app.toasts,
            ["The current sensor worker is still shutting down; profile unavailable"],
        )
        recorder_factory.assert_not_called()

    def test_construction_failure_leaves_no_stopped_profile_installed(self):
        app = self._make_app()
        current = _CurrentRecorder()
        current.shutdown_release.set()
        app.recorder = current

        with patch("fitness_tracker.ui.app.Recorder", side_effect=RuntimeError("build failed")):
            app.apply_sensor_settings()
            self.assertTrue(current.shutdown_started.wait(timeout=1))
            self._run_idle()

        self.assertIsNone(app.recorder)
        self.assertFalse(app.profile_installed.is_set())
        self.assertEqual(app.toasts, ["Unable to build the sensor worker: build failed"])

    def test_start_failure_leaves_profile_unavailable(self):
        app = self._make_app()
        app.test_mode = False
        current = _CurrentRecorder()
        current.shutdown_release.set()
        app.recorder = current
        replacement = types.SimpleNamespace(
            start=Mock(side_effect=RuntimeError("start failed")),
            shutdown=Mock(),
        )

        with patch("fitness_tracker.ui.app.Recorder", return_value=replacement):
            app.apply_sensor_settings()
            self.assertTrue(current.shutdown_started.wait(timeout=1))
            self._run_idle()

        self.assertIsNone(app.recorder)
        self.assertFalse(app._sensor_apply_running)
        self.assertFalse(app.profile_installed.is_set())
        replacement.shutdown.assert_called_once_with()
        self.assertEqual(app.toasts, ["Unable to start the sensor worker: start failed"])

    def test_failed_stale_sensor_request_resubmits_latest_generation(self):
        app = self._make_app()
        failed = _SensorProfileRequest(
            generation=1,
            sport_type=SportTypesEnum.running,
            trainer=False,
            profile=SensorProfile(hr_address="old"),
        )
        app._sensor_request = _SensorProfileRequest(
            generation=2,
            sport_type=SportTypesEnum.biking,
            trainer=False,
            profile=SensorProfile(hr_address="new"),
        )
        app._submit_sensor_apply = Mock()
        error = _SensorProfileApplyError(
            "old request failed",
            request=failed,
            current=None,
            clear_current=False,
        )

        app._on_sensor_apply_error(error)

        self.assertTrue(app._sensor_apply_running)
        app._submit_sensor_apply.assert_called_once_with()

    def test_failed_current_sensor_request_is_not_retried_immediately(self):
        app = self._make_app()
        request = _SensorProfileRequest(
            generation=1,
            sport_type=SportTypesEnum.running,
            trainer=False,
            profile=SensorProfile(hr_address="current"),
        )
        app._sensor_request = request
        app._submit_sensor_apply = Mock()
        error = _SensorProfileApplyError(
            "current request failed",
            request=request,
            current=None,
            clear_current=False,
        )

        app._on_sensor_apply_error(error)

        self.assertFalse(app._sensor_apply_running)
        app._submit_sensor_apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()

# ruff: noqa: ANN001, ANN201, D101, D102, E402, PT009, PT027, SLF001

import queue
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import gi
import gi.repository

gi.require_versions = lambda _versions: None


class _Application:
    pass


class _GLib:
    callbacks: queue.Queue = queue.Queue()

    @classmethod
    def idle_add(cls, callback, *args):
        cls.callbacks.put(lambda: callback(*args))
        return 1


gi.repository.Adw = types.SimpleNamespace(Application=_Application)
gi.repository.Gdk = types.SimpleNamespace()
gi.repository.GLib = _GLib
gi.repository.Gtk = types.SimpleNamespace()

pebble_bridge = types.ModuleType("pebble_bridge")
pebble_bridge.PebbleBridge = object
sys.modules["pebble_bridge"] = pebble_bridge

ui_history = types.ModuleType("fitness_tracker.ui_history")
ui_history.HistoryPageUI = object
sys.modules[ui_history.__name__] = ui_history

ui_settings = types.ModuleType("fitness_tracker.ui_settings")
ui_settings.AppSettings = object
ui_settings.SettingsPageUI = object
ui_settings.fallback_settings = lambda _path: None
sys.modules[ui_settings.__name__] = ui_settings

ui_tracker = types.ModuleType("fitness_tracker.ui_tracker")
ui_tracker.TrackerPageUI = object
sys.modules[ui_tracker.__name__] = ui_tracker

from fitness_tracker import ui as ui_module
from fitness_tracker.database import SportTypesEnum
from fitness_tracker.ui import FitnessAppUI, SensorProfile


class _CurrentRecorder:
    def __init__(self, result=True):
        self.result = result
        self.shutdown_started = threading.Event()
        self.shutdown_release = threading.Event()
        self.sport_type = SportTypesEnum.running
        self.hr_address = "old"
        self.speed_address = ""
        self.cadence_address = ""
        self.power_address = ""
        self.trainer_address = ""
        self.trainer_machine_type = None

    def shutdown(self):
        self.shutdown_started.set()
        self.shutdown_release.wait(timeout=2)
        return self.result


class SensorSettingsLifecycleTests(unittest.TestCase):
    def setUp(self):
        while not _GLib.callbacks.empty():
            _GLib.callbacks.get_nowait()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def _make_app(self):
        app = FitnessAppUI.__new__(FitnessAppUI)
        app._sensor_apply_lock = threading.Lock()
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
        )
        app._profile_from_sport_type = lambda _sport_type, trainer=False: SensorProfile(
            hr_address="new",
        )
        return app

    def _run_idle(self):
        _GLib.callbacks.get(timeout=2)()

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

    def test_ui_thread_handoff_times_out(self):
        with (
            patch.object(ui_module.GLib, "idle_add", return_value=1),
            patch.object(ui_module, "UI_THREAD_WAIT_TIMEOUT_S", 0),
            self.assertRaisesRegex(TimeoutError, "GTK main thread"),
        ):
            FitnessAppUI._run_on_ui_thread(lambda: None)

    def test_shutdown_and_rebuild_do_not_block_caller(self):
        app = self._make_app()
        current = _CurrentRecorder()
        replacement = types.SimpleNamespace()
        app.recorder = current

        with patch("fitness_tracker.ui.Recorder", return_value=replacement):
            app.apply_sensor_settings()
            self.assertTrue(current.shutdown_started.wait(timeout=1))
            self.assertIs(app.recorder, current)

            current.shutdown_release.set()
            self._run_idle()
            self._run_idle()

        self.assertTrue(app.profile_installed.wait(timeout=1))
        self.assertIs(app.recorder, replacement)

    def test_shutdown_failure_is_reported_on_ui_thread(self):
        app = self._make_app()
        current = _CurrentRecorder(result=False)
        current.shutdown_release.set()
        app.recorder = current

        with patch("fitness_tracker.ui.Recorder") as recorder_factory:
            app.apply_sensor_settings()
            self.assertTrue(current.shutdown_started.wait(timeout=1))
            self._run_idle()

        self.assertIs(app.recorder, current)
        self.assertEqual(
            app.toasts,
            ["The current sensor worker is still shutting down; profile unchanged"],
        )
        recorder_factory.assert_not_called()

    def test_construction_failure_leaves_no_stopped_profile_installed(self):
        app = self._make_app()
        current = _CurrentRecorder()
        current.shutdown_release.set()
        app.recorder = current

        with patch("fitness_tracker.ui.Recorder", side_effect=RuntimeError("build failed")):
            app.apply_sensor_settings()
            self.assertTrue(current.shutdown_started.wait(timeout=1))
            self._run_idle()
            self._run_idle()

        self.assertIsNone(app.recorder)
        self.assertFalse(app.profile_installed.is_set())
        self.assertEqual(app.toasts, ["Unable to build the sensor worker: build failed"])


if __name__ == "__main__":
    unittest.main()

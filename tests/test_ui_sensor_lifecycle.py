# ruff: noqa: ANN001, ANN201, D101, D102, E402, PT009, SLF001

import queue
import sys
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

    def _make_app(self):
        app = FitnessAppUI.__new__(FitnessAppUI)
        app._sensor_apply_lock = threading.Lock()
        app.app_settings = types.SimpleNamespace(
            personal=types.SimpleNamespace(weight_kg=75.0),
        )
        app.database = Path("/tmp/fitness-tracker-test.db")
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


if __name__ == "__main__":
    unittest.main()

# ruff: noqa: ANN001, ANN201, ANN202, D101, D102, E402, PT009, RUF006, SLF001

import asyncio
import threading
import types
import unittest

# Recorder only needs these modules for UI callbacks. Stub them so its
# event-loop lifecycle can be tested on headless systems without GTK typelibs.
import gi
import gi.repository

gi.require_versions = lambda _versions: None
gi.repository.Adw = types.SimpleNamespace()

from fitness_tracker.database import SportTypesEnum
from fitness_tracker.recorder import Recorder


def _make_recorder(*, test_mode=False):
    return Recorder(
        weight_kg=None,
        sport_type=SportTypesEnum.running,
        database_url="sqlite:///:memory:",
        hr_name=None,
        hr_address=None,
        speed_name=None,
        speed_address=None,
        cadence_name=None,
        cadence_address=None,
        power_name=None,
        power_address=None,
        trainer_name=None,
        trainer_address=None,
        trainer_machine_type=None,
        on_error=lambda _msg: None,
        test_mode=test_mode,
    )


class RecorderLifecycleTests(unittest.TestCase):
    def test_shutdown_cancels_remaining_tasks_and_closes_loop(self):
        recorder = _make_recorder()
        workflow_started = threading.Event()
        lingering_cancelled = threading.Event()

        async def fake_workflow():
            recorder._stop_event = asyncio.Event()
            workflow_started.set()

            async def lingering_task():
                try:
                    await asyncio.Event().wait()
                finally:
                    lingering_cancelled.set()

            asyncio.create_task(lingering_task())
            await recorder._wait_for_stop()

        recorder._workflow = fake_workflow
        recorder.start()
        self.assertTrue(workflow_started.wait(timeout=2.0))

        self.assertTrue(recorder.shutdown(timeout=2.0))
        self.assertTrue(lingering_cancelled.wait(timeout=2.0))
        self.assertTrue(recorder.loop.is_closed())
        self.assertFalse(recorder._thread.is_alive())

    def test_unstarted_test_recorder_closes_its_loop(self):
        recorder = _make_recorder(test_mode=True)

        self.assertTrue(recorder.shutdown())
        self.assertTrue(recorder.loop.is_closed())

    def test_trainer_target_can_switch_between_power_and_resistance(self):
        recorder = _make_recorder(test_mode=True)
        recorder._erg_disabled = False

        recorder.set_target_power(150)
        self.assertEqual(recorder._trainer_target_mode, "Power")
        self.assertEqual(recorder._pending_trainer_target, 150)

        recorder.set_target_resistance(25)
        self.assertEqual(recorder._trainer_target_mode, "Resistance")
        self.assertEqual(recorder._pending_trainer_target, 25)

        recorder.set_target_speed(8.5)
        self.assertEqual(recorder._trainer_target_mode, "Speed")
        self.assertEqual(recorder._pending_trainer_target, 8.5)

        recorder.shutdown()

    def test_erg_lockout_keeps_recovery_resistance_when_power_target_arrives(self):
        recorder = _make_recorder(test_mode=True)
        recorder.set_target_resistance(5)
        recorder._erg_disabled = True

        recorder.set_target_power(250)

        self.assertEqual(recorder._trainer_target_mode, "Resistance")
        self.assertEqual(recorder._pending_trainer_target, 5)
        self.assertEqual(recorder._erg_safeguard_saved_watts, 250)

        recorder.shutdown()


if __name__ == "__main__":
    unittest.main()

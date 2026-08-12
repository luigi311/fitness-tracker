# ruff: noqa: E402, PT009, RUF006, SLF001

import asyncio
import threading
import types
import unittest
from unittest.mock import Mock

# Recorder only needs these modules for UI callbacks. Stub them so its
# event-loop lifecycle can be tested on headless systems without GTK typelibs.
import gi
import gi.repository

gi.require_versions = lambda _versions: None
gi.repository.Adw = types.SimpleNamespace()

from bleaksport import HeartRateSample, TrainerSample
from fitness_tracker.core.sensor_profile import SensorProfile
from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.core.trainer_mode import TrainerMode
from fitness_tracker.database import DatabaseManager
from fitness_tracker.hardware.recorder import (
    FinalizationStatus,
    Recorder,
    RecorderSensorKind,
)


def _make_recorder(*, test_mode=False, trainer_supplied_hr=False, with_callback=True):
    return Recorder(
        profile=SensorProfile(trainer_supplied_hr=trainer_supplied_hr),
        weight_kg=None,
        sport_type=SportTypesEnum.running,
        database=DatabaseManager("sqlite:///:memory:"),
        on_error=lambda _msg: None,
        on_sample_update=(lambda _sample: None) if with_callback else None,
        test_mode=test_mode,
    )


class RecorderLifecycleTests(unittest.TestCase):
    def test_discovered_devices_match_the_profile_sensor_table(self):
        profile = SensorProfile(
            hr_address="hr-address",
            speed_name="speed-name",
            cadence_address="cadence-address",
            power_name="power-name",
            trainer_address="trainer-address",
        )
        recorder = Recorder(
            profile=profile,
            weight_kg=None,
            sport_type=SportTypesEnum.running,
            database=DatabaseManager("sqlite:///:memory:"),
            on_error=lambda _msg: None,
            test_mode=True,
        )
        devices = [
            types.SimpleNamespace(address="hr-address", name="unknown"),
            types.SimpleNamespace(address="unknown", name="speed-name"),
            types.SimpleNamespace(address="cadence-address", name="unknown"),
            types.SimpleNamespace(address="unknown", name="power-name"),
            types.SimpleNamespace(address="trainer-address", name="unknown"),
        ]
        recorder.devices = devices

        recorder._match_discovered_devices()

        for kind, device in zip(RecorderSensorKind, devices, strict=True):
            self.assertIs(recorder._configured_sensors[kind].device, device)
        self.assertTrue(recorder.trainer_configured)
        recorder.shutdown()

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

    def test_shutdown_surfaces_finalized_activity_id_once(self):
        recorder = _make_recorder()
        recorder.start_recording()
        activity_id = recorder.activity_id
        self.assertIsNotNone(activity_id)

        self.assertTrue(recorder.shutdown())
        self.assertEqual(recorder.take_shutdown_finalized_activity_id(), activity_id)
        self.assertIsNone(recorder.take_shutdown_finalized_activity_id())

    def test_shutdown_surfaces_activity_finalized_by_an_existing_worker(self):
        recorder = _make_recorder()
        recorder.start_recording()
        activity_id = recorder.activity_id
        self.assertIsNotNone(activity_id)
        claim = recorder.begin_finalization()
        self.assertIsNotNone(claim.activity_id)

        finalization_started = threading.Event()
        release_finalization = threading.Event()

        def finalize(_activity_id):
            finalization_started.set()
            self.assertTrue(release_finalization.wait(timeout=2.0))

        recorder.store.finalize_activity = finalize
        finalization_thread = threading.Thread(
            target=recorder.finish_finalization,
            args=(claim.activity_id,),
        )
        finalization_thread.start()
        self.assertTrue(finalization_started.wait(timeout=1.0))

        shutdown_claimed = threading.Event()
        original_begin_finalization = recorder.begin_finalization

        def begin_finalization():
            shutdown_claim = original_begin_finalization()
            if shutdown_claim.status is FinalizationStatus.ALREADY_RUNNING:
                shutdown_claimed.set()
            return shutdown_claim

        recorder.begin_finalization = begin_finalization
        shutdown_result: list[bool] = []
        shutdown_thread = threading.Thread(
            target=lambda: shutdown_result.append(recorder.shutdown(timeout=2.0)),
        )
        shutdown_thread.start()
        self.assertTrue(shutdown_claimed.wait(timeout=1.0))
        release_finalization.set()
        shutdown_thread.join(timeout=2.0)
        finalization_thread.join(timeout=2.0)

        self.assertEqual(shutdown_result, [True])
        self.assertEqual(recorder.take_shutdown_finalized_activity_id(), activity_id)
        self.assertTrue(recorder.claim_finalization_reconciliation(activity_id))
        self.assertFalse(recorder.claim_finalization_reconciliation(activity_id))

    def test_trainer_target_can_switch_between_power_and_resistance(self):
        recorder = _make_recorder(test_mode=True)
        recorder.trainer.erg_disabled = False

        recorder.set_target_power(150)
        self.assertEqual(recorder.trainer.target_mode, "Power")
        self.assertEqual(recorder.trainer.pending_target, 150)

        recorder.set_target_resistance(25)
        self.assertEqual(recorder.trainer.target_mode, "Resistance")
        self.assertEqual(recorder.trainer.pending_target, 25)

        recorder.set_target_speed(8.5)
        self.assertEqual(recorder.trainer.target_mode, "Speed")
        self.assertEqual(recorder.trainer.pending_target, 8.5)

        recorder.shutdown()

    def test_erg_lockout_keeps_recovery_resistance_when_power_target_arrives(self):
        recorder = _make_recorder(test_mode=True)
        recorder.set_target_resistance(5)
        recorder.trainer.erg_disabled = True

        recorder.set_target_power(250)

        self.assertEqual(recorder.trainer.target_mode, "Resistance")
        self.assertEqual(recorder.trainer.pending_target, 5)
        self.assertEqual(recorder.trainer.erg_safeguard_saved_watts, 250)

        recorder.shutdown()

    def test_erg_safeguard_recovers_from_an_applied_power_target(self):
        recorder = _make_recorder(test_mode=True)
        recorder.trainer.erg_disabled = False
        recorder.trainer.target_mode = TrainerMode.POWER
        recorder.trainer.pending_target = None
        recorder.trainer.erg_applied_target = 250
        recorder.trainer.test_mode = False
        recorder.trainer._set_target_resistance = Mock()

        recorder.trainer.update_erg_safeguard(0, 0)
        recorder.trainer.update_erg_safeguard(4000, 0)

        self.assertTrue(recorder.trainer.erg_disabled)
        self.assertEqual(recorder.trainer.erg_safeguard_saved_watts, 250)
        recorder.trainer._set_target_resistance.assert_called_once_with(
            5,
            preserve_erg_recovery=True,
        )

        recorder.shutdown()

    def test_erg_recovery_requires_three_seconds_above_threshold(self):
        recorder = _make_recorder(test_mode=True)
        recorder.trainer.target_mode = TrainerMode.POWER
        recorder.trainer.erg_disabled = True

        recorder.trainer.update_erg_safeguard(0, 0)
        recorder.trainer.update_erg_safeguard(4000, 100)
        self.assertTrue(recorder.trainer.erg_disabled)

        recorder.trainer.update_erg_safeguard(7000, 100)

        self.assertFalse(recorder.trainer.erg_disabled)
        recorder.shutdown()

    def test_erg_disable_requires_three_seconds_below_threshold(self):
        recorder = _make_recorder(test_mode=True)
        recorder.trainer.target_mode = TrainerMode.POWER
        recorder.trainer.erg_disabled = False

        recorder.trainer.update_erg_safeguard(0, 100)
        recorder.trainer.update_erg_safeguard(4000, 0)
        self.assertFalse(recorder.trainer.erg_disabled)

        recorder.trainer.update_erg_safeguard(7000, 0)

        self.assertTrue(recorder.trainer.erg_disabled)
        recorder.shutdown()

    def test_erg_safeguard_does_not_restore_resistance_as_power(self):
        recorder = _make_recorder(test_mode=True)
        recorder.trainer.sport_type = SportTypesEnum.biking
        recorder.trainer.erg_disabled = False
        recorder.trainer.target_mode = TrainerMode.RESISTANCE
        recorder.trainer.pending_target = 20
        recorder.trainer.erg_applied_target = 20
        recorder.trainer.test_mode = False
        recorder.trainer._set_target_resistance = Mock()
        recorder.trainer.set_target_power = Mock()

        recorder.trainer.update_erg_safeguard(0, 0)
        recorder.trainer.update_erg_safeguard(4000, 0)
        recorder.trainer.update_erg_safeguard(8000, 100)
        recorder.trainer.update_erg_safeguard(12000, 100)

        self.assertFalse(recorder.trainer.erg_disabled)
        self.assertIsNone(recorder.trainer.erg_safeguard_saved_watts)
        self.assertEqual(recorder.trainer.pending_target, 20)
        recorder.trainer._set_target_resistance.assert_not_called()
        recorder.trainer.set_target_power.assert_not_called()
        recorder.shutdown()

    def test_explicit_resistance_cancels_erg_recovery(self):
        recorder = _make_recorder(test_mode=True)
        recorder.trainer.sport_type = SportTypesEnum.biking
        recorder.trainer.erg_disabled = False
        recorder.trainer.target_mode = TrainerMode.POWER
        recorder.trainer.pending_target = None
        recorder.trainer.erg_applied_target = 250

        recorder.trainer.update_erg_safeguard(0, 0)
        recorder.trainer.update_erg_safeguard(4000, 0)
        self.assertEqual(recorder.trainer.erg_safeguard_saved_watts, 250)

        recorder.set_target_resistance(20)
        recorder.trainer.update_erg_safeguard(8000, 100)

        self.assertEqual(recorder.trainer.target_mode, TrainerMode.RESISTANCE)
        self.assertEqual(recorder.trainer.pending_target, 20)
        self.assertIsNone(recorder.trainer.erg_safeguard_saved_watts)
        recorder.shutdown()

    def test_in_flight_power_result_does_not_clear_new_resistance_target(self):
        recorder = _make_recorder()
        command_started = asyncio.Event()
        resistance_started = asyncio.Event()
        hold_resistance = asyncio.Event()

        class TrainerMuxMock:
            is_connected = True

            async def set_target_power(self, watts):
                command_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    return watts

            async def set_target_resistance(self, resistance):
                resistance_started.set()
                await hold_resistance.wait()
                return resistance

        async def exercise_race():
            recorder.loop.close()
            recorder.loop = asyncio.get_running_loop()
            recorder.trainer.trainer_mux = TrainerMuxMock()
            recorder.trainer.erg_disabled = False
            recorder.set_target_power(5)
            await command_started.wait()

            recorder.set_target_resistance(5)
            await resistance_started.wait()

            self.assertEqual(recorder.trainer.target_mode, "Resistance")
            self.assertEqual(recorder.trainer.pending_target, 5)

        asyncio.run(exercise_race())
        recorder.shutdown()

    def test_speed_target_is_ignored_by_erg_safeguard(self):
        recorder = _make_recorder(test_mode=True)
        recorder.set_target_speed(8.5)
        recorder.trainer.erg_disabled = False

        recorder.trainer.update_erg_safeguard(0, 0)
        recorder.trainer.update_erg_safeguard(4000, 0)

        self.assertFalse(recorder.trainer.erg_disabled)
        self.assertEqual(recorder.trainer.target_mode, "Speed")
        self.assertEqual(recorder.trainer.pending_target, 8.5)
        self.assertIsNone(recorder.trainer.erg_safeguard_saved_watts)

        recorder.trainer.erg_disabled = True
        recorder.trainer.erg_safeguard_saved_watts = 250
        recorder.trainer.update_erg_safeguard(8000, 100)
        recorder.trainer.update_erg_safeguard(12000, 100)

        self.assertTrue(recorder.trainer.erg_disabled)
        self.assertEqual(recorder.trainer.target_mode, "Speed")
        self.assertEqual(recorder.trainer.pending_target, 8.5)
        self.assertEqual(recorder.trainer.erg_safeguard_saved_watts, 250)

        recorder.shutdown()

    def test_trainer_supplied_heart_rate_is_forwarded_and_persisted(self):
        recorder = _make_recorder(test_mode=True, trainer_supplied_hr=True)
        recorder._recording = True
        recorder.activity_id = 1
        recorder.store.insert_heart_rate = Mock()
        recorder.store.insert_running_metrics = Mock()

        recorder.inject_test_sample(
            TrainerSample(timestamp_ms=1000, heart_rate_bpm=152),
        )

        self.assertTrue(recorder.hr_connected)
        recorder.store.insert_heart_rate.assert_called_once_with(1, 0, 152, None)

        recorder._recording = False
        recorder.shutdown()

    def test_stop_recording_waits_for_in_flight_sample_before_finalizing(self):
        recorder = _make_recorder(test_mode=True)
        recorder._recording = True
        recorder.activity_id = 1
        insert_started = threading.Event()
        release_insert = threading.Event()
        insert_finished = threading.Event()
        finalize_started = threading.Event()
        release_finalize = threading.Event()
        finalize_called = threading.Event()

        def insert_heart_rate(*_args):
            insert_started.set()
            self.assertTrue(release_insert.wait(timeout=2.0))
            insert_finished.set()

        def finalize_activity(activity_id):
            self.assertEqual(activity_id, 1)
            self.assertTrue(insert_finished.is_set())
            finalize_started.set()
            self.assertTrue(release_finalize.wait(timeout=2.0))
            finalize_called.set()

        recorder.store.insert_heart_rate = insert_heart_rate
        recorder.store.finalize_activity = finalize_activity
        sample_thread = threading.Thread(
            target=recorder._handle_hr_sample,
            args=(HeartRateSample(timestamp_ms=1_000, heart_rate_bpm=140),),
        )
        sample_thread.start()
        self.assertTrue(insert_started.wait(timeout=2.0))

        stop_thread = threading.Thread(target=recorder.stop_recording)
        stop_thread.start()
        self.assertFalse(finalize_called.wait(timeout=0.05))

        release_insert.set()
        sample_thread.join(timeout=2.0)
        self.assertTrue(finalize_started.wait(timeout=2.0))
        self.assertTrue(recorder._recording_lock.acquire(timeout=0.2))
        recorder._recording_lock.release()
        release_finalize.set()
        stop_thread.join(timeout=2.0)
        self.assertFalse(sample_thread.is_alive())
        self.assertFalse(stop_thread.is_alive())
        self.assertTrue(finalize_called.is_set())
        recorder.shutdown()

    def test_stop_recording_retries_failed_finalization(self):
        recorder = _make_recorder()
        recorder.store.start_activity = Mock(return_value=1)
        failure = RuntimeError("temporary finalization failure")
        recorder.store.finalize_activity = Mock(side_effect=[failure, None])

        recorder.start_recording()

        self.assertIsNone(recorder.stop_recording())
        self.assertFalse(recorder._recording)
        self.assertTrue(recorder.finalization_pending)
        self.assertEqual(recorder.activity_id, 1)

        self.assertEqual(recorder.stop_recording(), 1)
        self.assertFalse(recorder.finalization_pending)
        self.assertIsNone(recorder.activity_id)
        recorder.store.finalize_activity.assert_called_with(1)
        recorder.shutdown()

    def test_stop_recording_without_recording_clears_stale_activity_id(self):
        recorder = _make_recorder(test_mode=True)
        recorder.activity_id = 99

        self.assertIsNone(recorder.stop_recording())
        self.assertIsNone(recorder.activity_id)
        recorder.shutdown()

    def test_sample_from_previous_recording_is_not_persisted_after_restart(self):
        recorder = _make_recorder()
        recorder.store.start_activity = Mock(side_effect=[1, 2])
        recorder.store.finalize_activity = Mock()
        recorder.store.insert_heart_rate = Mock()
        normalization_started = threading.Event()
        release_normalization = threading.Event()
        dispatch_started = threading.Event()
        release_dispatch = threading.Event()
        original_clean = recorder._sample_processor.clean_heart_rate

        def blocked_clean(sample):
            normalization_started.set()
            release_normalization.wait(timeout=2.0)
            return original_clean(sample)

        def blocked_dispatch(_sample):
            dispatch_started.set()
            release_dispatch.wait(timeout=2.0)

        recorder._sample_processor.clean_heart_rate = blocked_clean
        recorder.on_sample = blocked_dispatch
        recorder.start_recording()
        sample_thread = threading.Thread(
            target=recorder._handle_hr_sample,
            args=(HeartRateSample(timestamp_ms=1_000, heart_rate_bpm=140),),
        )
        sample_thread.start()
        self.assertTrue(normalization_started.wait(timeout=1.0))

        release_normalization.set()
        self.assertTrue(dispatch_started.wait(timeout=1.0))
        stop_thread = threading.Thread(target=recorder.stop_recording)
        stop_thread.start()
        stop_thread.join(timeout=1.0)
        self.assertFalse(stop_thread.is_alive())
        recorder.start_recording()
        self.assertEqual(recorder.activity_id, 2)

        release_dispatch.set()
        sample_thread.join(timeout=2.0)
        self.assertFalse(sample_thread.is_alive())
        recorder.store.finalize_activity.assert_called_once_with(1)
        recorder.store.insert_heart_rate.assert_not_called()
        recorder.stop_recording()
        recorder.shutdown()

    def test_recording_pipeline_does_not_require_sample_callback(self):
        recorder = _make_recorder(
            test_mode=True,
            trainer_supplied_hr=True,
            with_callback=False,
        )
        recorder._recording = True
        recorder.activity_id = 1
        recorder.store.insert_heart_rate = Mock()
        recorder.store.insert_running_metrics = Mock()
        recorder.trainer.handle_sample = Mock()

        recorder.inject_test_sample(TrainerSample(timestamp_ms=1_000, heart_rate_bpm=145))

        recorder.trainer.handle_sample.assert_called_once()
        recorder.store.insert_heart_rate.assert_called_once()
        recorder.store.insert_running_metrics.assert_called_once()
        recorder._recording = False
        recorder.shutdown()

    def test_sample_dispatch_failure_does_not_prevent_persistence(self):
        recorder = _make_recorder()
        recorder._recording = True
        recorder.activity_id = 1
        recorder.store.insert_heart_rate = Mock()

        def fail_dispatch(_sample):
            message = "UI marshalling failed"
            raise RuntimeError(message)

        recorder.on_sample = fail_dispatch

        recorder._handle_hr_sample(HeartRateSample(timestamp_ms=1_000, heart_rate_bpm=140))

        recorder.store.insert_heart_rate.assert_called_once_with(1, 0, 140, None)
        recorder.shutdown()

    def test_target_heart_rate_requires_trainer_supplied_sample(self):
        recorder = _make_recorder(test_mode=True, trainer_supplied_hr=True)
        recorder.trainer.trainer_mux = types.SimpleNamespace(supports_target_heart_rate=True)

        self.assertFalse(recorder.set_target_heart_rate(150))
        self.assertFalse(recorder.trainer_heart_rate_control_available)
        self.assertIsNone(recorder.trainer.target_mode)

        recorder.inject_test_sample(TrainerSample(timestamp_ms=1000, heart_rate_bpm=145))

        self.assertTrue(recorder.trainer_heart_rate_control_available)
        self.assertTrue(recorder.set_target_heart_rate(150))
        self.assertEqual(recorder.trainer.target_mode, "HeartRate")
        self.assertEqual(recorder.trainer.pending_target, 150)
        recorder.shutdown()

    def test_target_heart_rate_ignores_trainer_sample_when_hrm_is_external(self):
        recorder = _make_recorder(test_mode=True, trainer_supplied_hr=False)
        recorder.trainer.trainer_mux = types.SimpleNamespace(supports_target_heart_rate=True)
        recorder.inject_test_sample(TrainerSample(timestamp_ms=1000, heart_rate_bpm=145))

        self.assertFalse(recorder.trainer_heart_rate_control_available)
        self.assertFalse(recorder.set_target_heart_rate(150))
        self.assertIsNone(recorder.trainer.target_mode)
        recorder.shutdown()

    def test_target_heart_rate_requires_trainer_control_support(self):
        recorder = _make_recorder(test_mode=True, trainer_supplied_hr=True)
        recorder.trainer.trainer_mux = types.SimpleNamespace(supports_target_heart_rate=False)
        recorder.inject_test_sample(TrainerSample(timestamp_ms=1000, heart_rate_bpm=145))

        self.assertFalse(recorder.trainer_heart_rate_control_available)
        self.assertFalse(recorder.set_target_heart_rate(150))
        self.assertIsNone(recorder.trainer.target_mode)
        recorder.shutdown()

    def test_trainer_disconnect_clears_pending_heart_rate_retry(self):
        recorder = _make_recorder(test_mode=True, trainer_supplied_hr=True)
        recorder.trainer.trainer_mux = types.SimpleNamespace(supports_target_heart_rate=True)
        recorder.inject_test_sample(TrainerSample(timestamp_ms=1000, heart_rate_bpm=145))
        self.assertTrue(recorder.set_target_heart_rate(150))
        recorder.trainer.erg_disabled = False
        recorder.trainer.erg_safeguard_saved_watts = 250
        recorder.trainer._power_above_since_ms = 100
        recorder.trainer._power_below_since_ms = 200
        retry_task = Mock()
        retry_task.done.return_value = False
        recorder.trainer.erg_retry_task = retry_task

        recorder._on_trainer_link("trainer", connected=False, _info={})

        self.assertIsNone(recorder.trainer.pending_target)
        self.assertIsNone(recorder.trainer.erg_retry_task)
        self.assertTrue(recorder.trainer.erg_disabled)
        self.assertIsNone(recorder.trainer.erg_safeguard_saved_watts)
        self.assertIsNone(recorder.trainer._power_above_since_ms)
        self.assertIsNone(recorder.trainer._power_below_since_ms)
        retry_task.cancel.assert_called_once_with()
        recorder.shutdown()

    def test_neutralize_biking_trainer_bypasses_erg_gating(self):
        recorder = _make_recorder(test_mode=True)
        recorder.trainer.sport_type = SportTypesEnum.biking
        recorder.trainer.target_mode = TrainerMode.RESISTANCE
        recorder.trainer.pending_target = 20
        recorder.trainer.erg_disabled = True
        recorder.trainer.erg_safeguard_saved_watts = 250

        recorder.neutralize_trainer()

        self.assertEqual(recorder.trainer.target_mode, TrainerMode.RESISTANCE)
        self.assertEqual(recorder.trainer.pending_target, 0)
        self.assertTrue(recorder.trainer.erg_disabled)
        self.assertIsNone(recorder.trainer.erg_safeguard_saved_watts)
        recorder.shutdown()


if __name__ == "__main__":
    unittest.main()

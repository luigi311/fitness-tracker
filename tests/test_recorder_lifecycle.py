# ruff: noqa: E402, PT009, RUF006, SLF001

import asyncio
import threading
import time
import types
import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import ANY, Mock, patch

# Recorder only needs these modules for UI callbacks. Stub them so its
# event-loop lifecycle can be tested on headless systems without GTK typelibs.
import gi
import gi.repository

gi.require_versions = lambda _versions: None
gi.repository.Adw = types.SimpleNamespace()

from bleaksport import HeartRateSample, TrainerSample
from fitness_tracker.core.environment import Environment
from fitness_tracker.core.sensor_profile import SensorProfile
from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.core.trainer_mode import TrainerMode
from fitness_tracker.database import DatabaseManager
from fitness_tracker.hardware import recorder as recorder_module
from fitness_tracker.hardware.location import (
    LocationFilter,
    LocationFix,
    LocationPolicy,
    LocationState,
)
from fitness_tracker.hardware.location_portal import PortalLocationSource
from fitness_tracker.hardware.recorder import (
    FinalizationStatus,
    Recorder,
    RecorderSensorKind,
)


def _make_recorder(*, test_mode=False, trainer_supplied_hr=False, with_callback=True, **kwargs):
    return Recorder(
        profile=SensorProfile(trainer_supplied_hr=trainer_supplied_hr),
        weight_kg=None,
        sport_type=SportTypesEnum.running,
        database=DatabaseManager("sqlite:///:memory:"),
        on_error=lambda _msg: None,
        on_sample_update=(lambda _sample: None) if with_callback else None,
        test_mode=test_mode,
        **kwargs,
    )


async def _wait_until(predicate: Callable[[], bool], *, wait_seconds: float = 2.0) -> None:
    """Yield until an observable async state is reached or a deadline expires."""
    deadline = time.monotonic() + wait_seconds
    while not predicate():
        if time.monotonic() >= deadline:
            message = "Timed out waiting for recorder lifecycle state"
            raise AssertionError(message)
        await asyncio.sleep(0.001)


class _RecorderLocationSource:
    """Controllable source double for recorder lifecycle tests."""

    def __init__(self) -> None:
        self.policy: LocationPolicy | None = None
        self.started = False
        self.start_count = 0
        self.stop_count = 0
        self.callbacks: list[
            tuple[
                Callable[[LocationFix], None],
                Callable[[LocationState, str | None], None],
            ]
        ] = []
        self.start_entered: asyncio.Event | None = None
        self.start_release: asyncio.Event | None = None
        self.thread_start_entered: threading.Event | None = None
        self.stop_loop_closed = False
        self.start_error: Exception | None = None
        self.stop_error: Exception | None = None

    async def start(
        self,
        policy: LocationPolicy,
        on_fix: Callable[[LocationFix], None],
        on_state: Callable[[LocationState, str | None], None],
    ) -> None:
        self.policy = policy
        self.callbacks.append((on_fix, on_state))
        self.start_count += 1
        if self.thread_start_entered is not None:
            self.thread_start_entered.set()
        if self.start_entered is not None:
            self.start_entered.set()
        if self.start_release is not None:
            await self.start_release.wait()
        if self.start_error is not None:
            raise self.start_error
        self.started = True
        on_state(LocationState.ACQUIRING, None)

    async def stop(self) -> None:
        self.stop_loop_closed = self._loop_is_closed()
        self.stop_count += 1
        if self.stop_error is not None:
            raise self.stop_error
        self.started = False

    @staticmethod
    def _loop_is_closed() -> bool:
        try:
            return asyncio.get_running_loop().is_closed()
        except RuntimeError:
            return True

    def emit(self, fix: LocationFix, callback_index: int = -1) -> None:
        self.callbacks[callback_index][0](fix)


class RecorderLifecycleTests(unittest.TestCase):
    _SECOND_SOURCE_OPERATION = 2

    def _make_recorder(self, **kwargs):
        recorder = _make_recorder(**kwargs)
        self.addCleanup(recorder.shutdown)
        return recorder

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
        self.addCleanup(recorder.shutdown)
        devices = [
            types.SimpleNamespace(address="hr-address", name="unknown"),
            types.SimpleNamespace(address="unknown", name="speed-name"),
            types.SimpleNamespace(address="cadence-address", name="unknown"),
            types.SimpleNamespace(address="unknown", name="power-name"),
            types.SimpleNamespace(address="trainer-address", name="unknown"),
        ]
        recorder.devices = devices

        recorder._match_discovered_devices()

        expected_devices = {
            RecorderSensorKind.HEART_RATE: devices[0],
            RecorderSensorKind.SPEED: devices[1],
            RecorderSensorKind.CADENCE: devices[2],
            RecorderSensorKind.POWER: devices[3],
            RecorderSensorKind.TRAINER: devices[4],
        }
        for kind, device in expected_devices.items():
            self.assertIs(recorder._configured_sensors[kind].device, device)
        self.assertTrue(recorder.trainer_configured)
        recorder.shutdown()

    def test_shutdown_cancels_remaining_tasks_and_closes_loop(self):
        recorder = self._make_recorder()
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
        recorder = self._make_recorder(test_mode=True)

        self.assertTrue(recorder.shutdown())
        self.assertTrue(recorder.loop.is_closed())

    def test_start_recording_forwards_environment_to_store(self):
        recorder = self._make_recorder()
        recorder.store.start_activity = Mock(return_value=1)
        recorder.store.finalize_activity = Mock()

        recorder.start_recording(Environment.OUTDOOR)

        recorder.store.start_activity.assert_called_once_with(
            sport_type=SportTypesEnum.running,
            environment=Environment.OUTDOOR,
        )
        recorder.stop_recording()
        recorder.shutdown()

    def test_outdoor_location_points_persist_and_report_state(self):
        source = _RecorderLocationSource()
        states: list[tuple[LocationState, str | None]] = []
        recorder = self._make_recorder(
            location_source=source,
            on_location_state=lambda state, detail: states.append((state, detail)),
        )
        recorder.store.start_activity = Mock(return_value=7)
        recorder.store.insert_location_point = Mock()
        recorder.store.finalize_activity = Mock()

        async def exercise() -> None:
            recorder.loop.close()
            recorder.loop = asyncio.get_running_loop()
            recorder.start_recording(Environment.OUTDOOR)
            await _wait_until(lambda: source.start_count == 1)

            self.assertEqual(source.policy, LocationPolicy.outdoor())
            source.emit(LocationFix(latitude_deg=39.7392, longitude_deg=-104.9903))
            source.emit(LocationFix(latitude_deg=39.7393, longitude_deg=-104.9903))
            self.assertEqual(recorder.store.insert_location_point.call_count, 2)

            self.assertEqual(recorder.stop_recording(), 7)
            await _wait_until(lambda: source.stop_count == 1)

        asyncio.run(exercise())

        self.assertEqual(
            states,
            [
                (LocationState.STARTING, None),
                (LocationState.ACQUIRING, None),
                (LocationState.TRACKING, None),
                (LocationState.STOPPED, None),
            ],
        )
        self.assertTrue(recorder.shutdown())

    def test_indoor_anchor_persists_once_and_stops_source(self):
        source = _RecorderLocationSource()
        recorder = self._make_recorder(
            location_source=source,
            record_indoor_anchor=True,
        )
        recorder.store.start_activity = Mock(return_value=8)
        recorder.store.insert_location_point = Mock()
        recorder.store.finalize_activity = Mock()

        async def exercise() -> None:
            recorder.loop.close()
            recorder.loop = asyncio.get_running_loop()
            recorder.start_recording(Environment.INDOOR)
            await _wait_until(lambda: source.start_count == 1)

            self.assertEqual(source.policy, LocationPolicy.anchor())
            source.emit(
                LocationFix(
                    latitude_deg=38.0000,
                    longitude_deg=-105.0000,
                    accuracy_m=130_000.0,
                    source_time_utc=datetime.now(UTC),
                ),
            )
            source.emit(
                LocationFix(
                    latitude_deg=39.7392,
                    longitude_deg=-104.9903,
                    accuracy_m=100.0,
                    source_time_utc=datetime.now(UTC),
                ),
            )
            source.emit(
                LocationFix(
                    latitude_deg=39.7393,
                    longitude_deg=-104.9903,
                    accuracy_m=100.0,
                    source_time_utc=datetime.now(UTC),
                ),
            )
            await _wait_until(lambda: source.stop_count == 1)

            self.assertEqual(recorder.store.insert_location_point.call_count, 1)
            self.assertEqual(source.stop_count, 1)
            self.assertEqual(recorder._location_points_accepted, 1)

            self.assertEqual(recorder.stop_recording(), 8)

        asyncio.run(exercise())
        self.assertTrue(recorder.shutdown())

    def test_indoor_and_trainer_anchor_are_disabled_by_default(self):
        for environment in (Environment.INDOOR, Environment.TRAINER):
            with self.subTest(environment=environment):
                source = _RecorderLocationSource()
                recorder = self._make_recorder(location_source=source)

                recorder.start_recording(environment)

                self.assertIsNone(source.policy)
                self.assertEqual(source.start_count, 0)
                self.assertIsNone(recorder.location_policy)
                self.assertEqual(recorder.location_state, LocationState.DISABLED)
                self.assertTrue(recorder.shutdown())

    def test_indoor_anchor_times_out_without_acceptable_fix(self):
        source = _RecorderLocationSource()
        states: list[tuple[LocationState, str | None]] = []
        recorder = self._make_recorder(
            location_source=source,
            record_indoor_anchor=True,
            on_location_state=lambda state, detail: states.append((state, detail)),
        )
        recorder.store.start_activity = Mock(return_value=20)
        recorder.store.insert_location_point = Mock()
        recorder.store.finalize_activity = Mock()
        timeout_policy = LocationPolicy(
            accuracy=LocationPolicy.anchor().accuracy,
            time_threshold_s=0,
            distance_threshold_m=0,
            max_points=1,
            max_accuracy_m=5_000.0,
            max_fix_age_s=300,
            acquisition_timeout_s=0.01,
        )

        async def exercise() -> None:
            recorder.loop.close()
            recorder.loop = asyncio.get_running_loop()
            with patch.object(
                recorder_module,
                "location_policy_for_environment",
                return_value=timeout_policy,
            ):
                recorder.start_recording(Environment.INDOOR)
            await _wait_until(lambda: source.start_count == 1)

            source.emit(
                LocationFix(
                    latitude_deg=39.7392,
                    longitude_deg=-104.9903,
                    accuracy_m=130_000.0,
                    source_time_utc=datetime.now(UTC),
                ),
            )
            await _wait_until(lambda: source.stop_count == 1)

            recorder.store.insert_location_point.assert_not_called()
            self.assertEqual(recorder._location_points_accepted, 0)
            self.assertEqual(recorder.stop_recording(), 20)

        asyncio.run(exercise())
        self.assertIn(
            (
                LocationState.UNAVAILABLE,
                "Location acquisition timed out without an acceptable fix",
            ),
            states,
        )
        self.assertTrue(recorder.shutdown())

    def test_trainer_anchor_persists_once_and_stops_source(self):
        source = _RecorderLocationSource()
        recorder = self._make_recorder(
            location_source=source,
            record_indoor_anchor=True,
        )
        recorder.store.start_activity = Mock(return_value=16)
        recorder.store.insert_location_point = Mock()
        recorder.store.finalize_activity = Mock()

        async def exercise() -> None:
            recorder.loop.close()
            recorder.loop = asyncio.get_running_loop()
            recorder.start_recording(Environment.TRAINER)
            await _wait_until(lambda: source.start_count == 1)

            self.assertEqual(source.policy, LocationPolicy.anchor())
            source.emit(
                LocationFix(
                    latitude_deg=39.7392,
                    longitude_deg=-104.9903,
                    accuracy_m=100.0,
                    source_time_utc=datetime.now(UTC),
                ),
            )
            await _wait_until(lambda: source.stop_count == 1)

            self.assertEqual(recorder.store.insert_location_point.call_count, 1)
            self.assertEqual(source.stop_count, 1)
            self.assertEqual(recorder.stop_recording(), 16)

        asyncio.run(exercise())
        self.assertTrue(recorder.shutdown())

    def test_test_mode_does_not_construct_the_desktop_portal_source(self):
        with patch.object(recorder_module, "PortalLocationSource") as portal_source:
            recorder = self._make_recorder(test_mode=True)
            recorder.start_recording(Environment.OUTDOOR)

            portal_source.assert_not_called()
            self.assertEqual(recorder.location_state, LocationState.DISABLED)
            self.assertTrue(recorder.shutdown())

    def test_stale_location_stop_cannot_cancel_next_recording_start(self):
        source = _RecorderLocationSource()
        recorder = self._make_recorder(location_source=source)
        recorder.store.start_activity = Mock(side_effect=[17, 18])
        recorder.store.insert_location_point = Mock()
        recorder.store.finalize_activity = Mock()

        async def exercise() -> None:
            recorder.loop.close()
            recorder.loop = asyncio.get_running_loop()
            recorder.start_recording(Environment.OUTDOOR)
            await _wait_until(lambda: source.start_count == 1)
            self.assertEqual(source.start_count, 1)

            self.assertEqual(recorder.stop_recording(), 17)
            recorder.start_recording(Environment.OUTDOOR)
            await _wait_until(lambda: source.start_count == self._SECOND_SOURCE_OPERATION)

            self.assertEqual(source.start_count, 2)
            self.assertEqual(source.stop_count, 1)
            self.assertEqual(recorder.location_state, LocationState.ACQUIRING)

            source.emit(LocationFix(latitude_deg=39.7392, longitude_deg=-104.9903))
            self.assertEqual(recorder.store.insert_location_point.call_count, 1)
            recorder.store.insert_location_point.assert_called_once_with(
                18,
                ANY,
                LocationFix(latitude_deg=39.7392, longitude_deg=-104.9903),
            )
            self.assertEqual(recorder.stop_recording(), 18)
            await _wait_until(lambda: source.stop_count == self._SECOND_SOURCE_OPERATION)

        asyncio.run(exercise())
        self.assertTrue(recorder.shutdown())

    def test_old_generation_location_fix_cannot_enter_new_activity(self):
        source = _RecorderLocationSource()
        recorder = self._make_recorder(location_source=source)
        recorder.store.start_activity = Mock(side_effect=[11, 12])
        recorder.store.insert_location_point = Mock()
        recorder.store.finalize_activity = Mock()

        async def exercise() -> None:
            recorder.loop.close()
            recorder.loop = asyncio.get_running_loop()
            recorder.start_recording(Environment.OUTDOOR)
            await _wait_until(lambda: source.start_count == 1)
            old_callback = source.callbacks[0][0]

            self.assertEqual(recorder.stop_recording(), 11)
            await _wait_until(lambda: source.stop_count == 1)

            recorder.start_recording(Environment.OUTDOOR)
            await _wait_until(lambda: source.start_count == self._SECOND_SOURCE_OPERATION)
            new_callback = source.callbacks[1][0]

            old_callback(LocationFix(latitude_deg=39.7392, longitude_deg=-104.9903))
            new_callback(LocationFix(latitude_deg=39.7393, longitude_deg=-104.9903))
            self.assertEqual(recorder.store.insert_location_point.call_count, 1)
            recorder.store.insert_location_point.assert_called_once_with(
                12,
                ANY,
                LocationFix(latitude_deg=39.7393, longitude_deg=-104.9903),
            )
            self.assertEqual(recorder.stop_recording(), 12)
            await _wait_until(lambda: source.stop_count == self._SECOND_SOURCE_OPERATION)

        asyncio.run(exercise())
        self.assertTrue(recorder.shutdown())

    def test_location_start_and_stop_failures_do_not_fail_finalization(self):
        source = _RecorderLocationSource()
        source.start_error = RuntimeError("portal unavailable")
        source.stop_error = RuntimeError("portal already closed")
        states: list[tuple[LocationState, str | None]] = []
        recorder = self._make_recorder(
            location_source=source,
            on_location_state=lambda state, detail: states.append((state, detail)),
        )
        recorder.store.start_activity = Mock(return_value=13)
        recorder.store.finalize_activity = Mock()

        async def exercise() -> None:
            recorder.loop.close()
            recorder.loop = asyncio.get_running_loop()
            recorder.start_recording(Environment.OUTDOOR)
            await _wait_until(lambda: recorder.location_state is LocationState.UNAVAILABLE)

            self.assertEqual(recorder.location_state, LocationState.UNAVAILABLE)
            self.assertEqual(recorder.stop_recording(), 13)
            await _wait_until(lambda: source.stop_count == 1)

        asyncio.run(exercise())

        self.assertIn(LocationState.UNAVAILABLE, [state for state, _detail in states])
        recorder.store.finalize_activity.assert_called_once_with(13)
        self.assertTrue(recorder.shutdown())

    def test_stop_during_location_start_invalidates_pending_callbacks(self):
        source = _RecorderLocationSource()
        recorder = self._make_recorder(location_source=source)
        recorder.store.start_activity = Mock(return_value=14)
        recorder.store.finalize_activity = Mock()
        start_entered = asyncio.Event()
        start_release = asyncio.Event()
        source.start_entered = start_entered
        source.start_release = start_release
        states: list[tuple[LocationState, str | None]] = []
        recorder.on_location_state = lambda state, detail: states.append((state, detail))

        async def exercise() -> None:
            recorder.loop.close()
            recorder.loop = asyncio.get_running_loop()
            recorder.start_recording(Environment.OUTDOOR)
            await start_entered.wait()

            self.assertEqual(recorder.stop_recording(), 14)
            start_release.set()
            await _wait_until(lambda: source.stop_count == 1)

        asyncio.run(exercise())

        self.assertEqual(source.start_count, 1)
        self.assertEqual(source.stop_count, 1)
        self.assertNotIn(LocationState.ACQUIRING, [state for state, _detail in states])
        self.assertTrue(recorder.shutdown())

    def test_shutdown_stops_location_source_before_closing_event_loop(self):
        source = _RecorderLocationSource()
        source.thread_start_entered = threading.Event()
        recorder = self._make_recorder(location_source=source)
        workflow_started = threading.Event()

        async def fake_workflow():
            recorder._stop_event = asyncio.Event()
            workflow_started.set()
            await recorder._wait_for_stop()

        recorder._workflow = fake_workflow
        recorder.start()
        self.assertTrue(workflow_started.wait(timeout=2.0))
        recorder.start_recording(Environment.OUTDOOR)
        self.assertTrue(source.thread_start_entered.wait(timeout=2.0))

        self.assertTrue(recorder.shutdown(timeout=2.0))
        self.assertEqual(source.stop_count, 1)
        self.assertFalse(source.stop_loop_closed)

    def test_shutdown_loop_stops_completed_source_when_worker_loop_is_stopped(self):
        source = _RecorderLocationSource()
        recorder = self._make_recorder(location_source=source)
        recorder.store.start_activity = Mock(return_value=19)
        recorder.store.finalize_activity = Mock()

        recorder.start_recording(Environment.OUTDOOR)
        recorder.loop.run_until_complete(_wait_until(lambda: source.started))

        self.assertEqual(recorder.stop_recording(), 19)
        self.assertFalse(recorder.loop.is_running())
        self.assertTrue(recorder._location_source_pending)
        self.assertEqual(source.stop_count, 0)

        recorder.loop.run_until_complete(recorder._shutdown_loop())

        self.assertEqual(source.stop_count, 1)
        self.assertFalse(source.stop_loop_closed)
        self.assertFalse(recorder._location_source_pending)

    def test_shutdown_recovers_from_wedged_portal_startup(self):
        class HangingBusFactory:
            def __init__(self) -> None:
                self.entered = threading.Event()

            async def __call__(self):
                self.entered.set()
                await asyncio.Event().wait()

        factory = HangingBusFactory()
        source = PortalLocationSource(bus_factory=factory)
        recorder = self._make_recorder(location_source=source)
        workflow_started = threading.Event()

        async def fake_workflow():
            recorder._stop_event = asyncio.Event()
            workflow_started.set()
            await recorder._wait_for_stop()

        recorder._workflow = fake_workflow
        recorder.start_recording(Environment.OUTDOOR)
        recorder.start()
        self.assertTrue(workflow_started.wait(timeout=2.0))
        self.assertTrue(factory.entered.wait(timeout=2.0))

        self.assertTrue(recorder.shutdown(timeout=3.0))

        self.assertTrue(source._stopped)
        self.assertTrue(recorder.loop.is_closed())
        self.assertFalse(recorder._thread.is_alive())

    def test_location_start_queued_before_worker_loop_starts(self):
        source = _RecorderLocationSource()
        source.thread_start_entered = threading.Event()
        recorder = self._make_recorder(location_source=source)
        recorder.store.start_activity = Mock(return_value=15)
        recorder.store.finalize_activity = Mock()
        workflow_started = threading.Event()

        async def fake_workflow():
            recorder._stop_event = asyncio.Event()
            workflow_started.set()
            await recorder._wait_for_stop()

        recorder._workflow = fake_workflow
        recorder.start_recording(Environment.OUTDOOR)
        self.assertEqual(recorder.location_state, LocationState.STARTING)
        recorder.start()

        self.assertTrue(workflow_started.wait(timeout=2.0))
        self.assertTrue(source.thread_start_entered.wait(timeout=2.0))
        self.assertEqual(source.start_count, 1)
        self.assertEqual(source.policy, LocationPolicy.outdoor())

        self.assertTrue(recorder.shutdown(timeout=2.0))

    def test_shutdown_surfaces_finalized_activity_id_once(self):
        recorder = self._make_recorder()
        recorder.start_recording(Environment.INDOOR)
        activity_id = recorder.activity_id
        self.assertIsNotNone(activity_id)

        self.assertTrue(recorder.shutdown())
        self.assertEqual(recorder.take_shutdown_finalized_activity_id(), activity_id)
        self.assertIsNone(recorder.take_shutdown_finalized_activity_id())

    def test_shutdown_surfaces_activity_finalized_by_an_existing_worker(self):
        recorder = self._make_recorder()
        recorder.start_recording(Environment.INDOOR)
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
        recorder = self._make_recorder(test_mode=True)
        recorder.trainer.erg_disabled = False

        recorder.set_target_power(150)
        self.assertEqual(recorder.trainer.target_mode, TrainerMode.POWER)
        self.assertEqual(recorder.trainer.pending_target, 150)

        recorder.set_target_resistance(25)
        self.assertEqual(recorder.trainer.target_mode, TrainerMode.RESISTANCE)
        self.assertEqual(recorder.trainer.pending_target, 25)

        recorder.set_target_speed(8.5)
        self.assertEqual(recorder.trainer.target_mode, TrainerMode.SPEED)
        self.assertEqual(recorder.trainer.pending_target, 8.5)

        recorder.shutdown()

    def test_erg_lockout_keeps_recovery_resistance_when_power_target_arrives(self):
        recorder = self._make_recorder(test_mode=True)
        recorder.set_target_resistance(5)
        recorder.trainer.erg_disabled = True

        recorder.set_target_power(250)

        self.assertEqual(recorder.trainer.target_mode, TrainerMode.RESISTANCE)
        self.assertEqual(recorder.trainer.pending_target, 5)
        self.assertEqual(recorder.trainer.erg_safeguard_saved_watts, 250)

        recorder.shutdown()

    def test_erg_safeguard_recovers_from_an_applied_power_target(self):
        recorder = self._make_recorder(test_mode=True)
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
        recorder = self._make_recorder(test_mode=True)
        recorder.trainer.target_mode = TrainerMode.POWER
        recorder.trainer.erg_disabled = True

        recorder.trainer.update_erg_safeguard(0, 0)
        recorder.trainer.update_erg_safeguard(4000, 100)
        self.assertTrue(recorder.trainer.erg_disabled)

        recorder.trainer.update_erg_safeguard(7000, 100)

        self.assertFalse(recorder.trainer.erg_disabled)
        recorder.shutdown()

    def test_erg_disable_requires_three_seconds_below_threshold(self):
        recorder = self._make_recorder(test_mode=True)
        recorder.trainer.target_mode = TrainerMode.POWER
        recorder.trainer.erg_disabled = False

        recorder.trainer.update_erg_safeguard(0, 100)
        recorder.trainer.update_erg_safeguard(4000, 0)
        self.assertFalse(recorder.trainer.erg_disabled)

        recorder.trainer.update_erg_safeguard(7000, 0)

        self.assertTrue(recorder.trainer.erg_disabled)
        recorder.shutdown()

    def test_erg_safeguard_does_not_restore_resistance_as_power(self):
        recorder = self._make_recorder(test_mode=True)
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
        recorder = self._make_recorder(test_mode=True)
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
        recorder = self._make_recorder()
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

            self.assertEqual(recorder.trainer.target_mode, TrainerMode.RESISTANCE)
            self.assertEqual(recorder.trainer.pending_target, 5)

        asyncio.run(exercise_race())
        recorder.shutdown()

    def test_speed_target_is_ignored_by_erg_safeguard(self):
        recorder = self._make_recorder(test_mode=True)
        recorder.set_target_speed(8.5)
        recorder.trainer.erg_disabled = False

        recorder.trainer.update_erg_safeguard(0, 0)
        recorder.trainer.update_erg_safeguard(4000, 0)

        self.assertFalse(recorder.trainer.erg_disabled)
        self.assertEqual(recorder.trainer.target_mode, TrainerMode.SPEED)
        self.assertEqual(recorder.trainer.pending_target, 8.5)
        self.assertIsNone(recorder.trainer.erg_safeguard_saved_watts)

        recorder.trainer.erg_disabled = True
        recorder.trainer.erg_safeguard_saved_watts = 250
        recorder.trainer.update_erg_safeguard(8000, 100)
        recorder.trainer.update_erg_safeguard(12000, 100)

        self.assertTrue(recorder.trainer.erg_disabled)
        self.assertEqual(recorder.trainer.target_mode, TrainerMode.SPEED)
        self.assertEqual(recorder.trainer.pending_target, 8.5)
        self.assertEqual(recorder.trainer.erg_safeguard_saved_watts, 250)

        recorder.shutdown()

    def test_metric_connectivity_combines_sensor_and_trainer_links(self):
        recorder = self._make_recorder(test_mode=True)

        recorder._on_running_link("sensor", connected=True, roles={"rsc": True, "cps": False})
        recorder._on_trainer_link("trainer", connected=True, _info={})
        recorder._on_running_link("sensor", connected=False, roles={})

        self.assertTrue(recorder.speed_connected)
        self.assertTrue(recorder.cadence_connected)
        self.assertTrue(recorder.distance_connected)
        self.assertTrue(recorder.power_connected)

        recorder._on_running_link("sensor", connected=True, roles={"rsc": True, "cps": False})
        recorder._on_trainer_link("trainer", connected=False, _info={})

        self.assertTrue(recorder.speed_connected)
        self.assertTrue(recorder.cadence_connected)
        self.assertTrue(recorder.distance_connected)
        self.assertFalse(recorder.power_connected)
        recorder.shutdown()

    def test_trainer_supplied_heart_rate_is_forwarded_and_persisted(self):
        recorder = self._make_recorder(test_mode=True, trainer_supplied_hr=True)
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
        recorder = self._make_recorder(test_mode=True)
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

    def test_location_persistence_does_not_hold_recording_lock(self):
        recorder = self._make_recorder(test_mode=True)
        recorder._recording = True
        recorder.activity_id = 1
        recorder._recording_origin_ns = time.monotonic_ns()
        recorder._location_filter = LocationFilter(
            LocationPolicy.outdoor(),
            max_speed_mps=12.0,
        )
        lock_available = threading.Event()
        recorder.store.insert_location_point = Mock(
            side_effect=lambda *_args: self._assert_recording_lock_available(
                recorder,
                lock_available,
            ),
        )

        fix = LocationFix(latitude_deg=39.7392, longitude_deg=-104.9903)
        with patch.object(recorder_module, "logger") as trace_logger:
            recorder._handle_location_fix(
                recorder._recording_generation,
                recorder._location_operation,
                fix,
            )

        self.assertTrue(lock_available.is_set())
        trace_logger.bind.assert_any_call(data={"timestamp_ms": ANY, "fix": fix})
        trace_logger.bind.return_value.trace.assert_any_call("Persisted location fix")
        recorder._recording = False
        recorder.shutdown()

    def test_stale_persistence_callback_does_not_wait_for_finalization(self):
        recorder = self._make_recorder()
        recorder._recording = True
        recorder.activity_id = 1
        generation = recorder._recording_generation
        finalize_started = threading.Event()
        release_finalize = threading.Event()

        def finalize_activity(_activity_id):
            finalize_started.set()
            release_finalize.wait(timeout=2.0)

        recorder.store.finalize_activity = finalize_activity
        self.assertEqual(
            recorder.begin_finalization().status,
            FinalizationStatus.STARTED,
        )
        finalization_thread = threading.Thread(
            target=recorder.finish_finalization,
            args=(1,),
        )
        finalization_thread.start()
        self.assertTrue(finalize_started.wait(timeout=1.0))

        callback_finished = threading.Event()
        callback_result: list[bool] = []
        persistence_called = threading.Event()

        def stale_callback() -> None:
            callback_result.append(
                recorder._persist_if_recording(
                    generation,
                    lambda _activity_id: persistence_called.set(),
                ),
            )
            callback_finished.set()

        callback_thread = threading.Thread(target=stale_callback)
        callback_thread.start()
        self.assertTrue(callback_finished.wait(timeout=0.2))
        callback_thread.join(timeout=1.0)

        release_finalize.set()
        finalization_thread.join(timeout=2.0)
        self.assertFalse(finalization_thread.is_alive())
        self.assertEqual(callback_result, [False])
        self.assertFalse(persistence_called.is_set())
        recorder.shutdown()

    @staticmethod
    def _assert_recording_lock_available(
        recorder: Recorder,
        lock_available: threading.Event,
    ) -> None:
        acquired = recorder._recording_lock.acquire(timeout=0.2)
        if acquired:
            recorder._recording_lock.release()
            lock_available.set()

    def test_stop_recording_retries_failed_finalization(self):
        recorder = self._make_recorder()
        recorder.store.start_activity = Mock(return_value=1)
        failure = RuntimeError("temporary finalization failure")
        recorder.store.finalize_activity = Mock(side_effect=[failure, None])

        recorder.start_recording(Environment.INDOOR)

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
        recorder = self._make_recorder(test_mode=True)
        recorder.activity_id = 99

        self.assertIsNone(recorder.stop_recording())
        self.assertIsNone(recorder.activity_id)
        recorder.shutdown()

    def test_sample_from_previous_recording_is_not_persisted_after_restart(self):
        recorder = self._make_recorder()
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
        recorder.start_recording(Environment.INDOOR)
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
        recorder.start_recording(Environment.INDOOR)
        self.assertEqual(recorder.activity_id, 2)

        release_dispatch.set()
        sample_thread.join(timeout=2.0)
        self.assertFalse(sample_thread.is_alive())
        recorder.store.finalize_activity.assert_called_once_with(1)
        recorder.store.insert_heart_rate.assert_not_called()
        recorder.stop_recording()
        recorder.shutdown()

    def test_recording_pipeline_does_not_require_sample_callback(self):
        recorder = self._make_recorder(
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
        recorder = self._make_recorder()
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
        recorder = self._make_recorder(test_mode=True, trainer_supplied_hr=True)
        recorder.trainer.trainer_mux = types.SimpleNamespace(supports_target_heart_rate=True)

        self.assertFalse(recorder.set_target_heart_rate(150))
        self.assertFalse(recorder.trainer_heart_rate_control_available)
        self.assertIsNone(recorder.trainer.target_mode)

        recorder.inject_test_sample(TrainerSample(timestamp_ms=1000, heart_rate_bpm=145))

        self.assertTrue(recorder.trainer_heart_rate_control_available)
        self.assertTrue(recorder.set_target_heart_rate(150))
        self.assertEqual(recorder.trainer.target_mode, TrainerMode.HEART_RATE)
        self.assertEqual(recorder.trainer.pending_target, 150)
        recorder.shutdown()

    def test_target_heart_rate_ignores_trainer_sample_when_hrm_is_external(self):
        recorder = self._make_recorder(test_mode=True, trainer_supplied_hr=False)
        recorder.trainer.trainer_mux = types.SimpleNamespace(supports_target_heart_rate=True)
        recorder.inject_test_sample(TrainerSample(timestamp_ms=1000, heart_rate_bpm=145))

        self.assertFalse(recorder.trainer_heart_rate_control_available)
        self.assertFalse(recorder.set_target_heart_rate(150))
        self.assertIsNone(recorder.trainer.target_mode)
        recorder.shutdown()

    def test_target_heart_rate_requires_trainer_control_support(self):
        recorder = self._make_recorder(test_mode=True, trainer_supplied_hr=True)
        recorder.trainer.trainer_mux = types.SimpleNamespace(supports_target_heart_rate=False)
        recorder.inject_test_sample(TrainerSample(timestamp_ms=1000, heart_rate_bpm=145))

        self.assertFalse(recorder.trainer_heart_rate_control_available)
        self.assertFalse(recorder.set_target_heart_rate(150))
        self.assertIsNone(recorder.trainer.target_mode)
        recorder.shutdown()

    def test_trainer_disconnect_clears_pending_heart_rate_retry(self):
        recorder = self._make_recorder(test_mode=True, trainer_supplied_hr=True)
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

    def test_replacing_trainer_mux_clears_heart_rate_target_availability(self):
        recorder = self._make_recorder(test_mode=True, trainer_supplied_hr=True)
        recorder.trainer.trainer_mux = types.SimpleNamespace(supports_target_heart_rate=True)
        recorder.inject_test_sample(TrainerSample(timestamp_ms=1000, heart_rate_bpm=145))
        self.assertTrue(recorder.set_target_heart_rate(150))

        recorder.trainer.trainer_mux = types.SimpleNamespace(supports_target_heart_rate=True)

        self.assertFalse(recorder.trainer_heart_rate_control_available)
        self.assertIsNone(recorder.trainer.pending_target)
        self.assertFalse(recorder.set_target_heart_rate(150))
        recorder.shutdown()

    def test_neutralize_biking_trainer_bypasses_erg_gating(self):
        recorder = self._make_recorder(test_mode=True)
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

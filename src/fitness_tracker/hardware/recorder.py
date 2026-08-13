"""BLE recorder orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from bleak import BleakScanner
from bleaksport import (
    CyclingMux,
    HeartRateMux,
    HeartRateSample,
    MachineType,
    RunningMux,
    RunningSample,
    TrainerMux,
    TrainerSample,
)
from bleaksport.models import CyclingSample
from loguru import logger

from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.hardware.processor import SampleProcessor
from fitness_tracker.hardware.trainer import TrainerController

if TYPE_CHECKING:
    from collections.abc import Callable

    from bleak.backends.device import BLEDevice

    from fitness_tracker.core.sensor_profile import SensorProfile
    from fitness_tracker.hardware.store import RecordingStore


class RecorderSensorKind(StrEnum):
    """Sensor roles that can be matched during the initial BLE scan."""

    HEART_RATE = "HR"
    SPEED = "speed"
    CADENCE = "cadence"
    POWER = "power"
    TRAINER = "trainer"


class FinalizationStatus(StrEnum):
    """Outcome of attempting to claim an activity for finalization."""

    STARTED = "started"
    ALREADY_RUNNING = "already-running"
    NOT_RECORDING = "not-recording"
    NO_ACTIVITY = "no-activity"


@dataclass(frozen=True)
class FinalizationClaim:
    """Result of a finalization claim, including its activity when available."""

    status: FinalizationStatus
    activity_id: int | None = None


@dataclass
class _ConfiguredSensor:
    name: str | None
    address: str | None
    device: BLEDevice | None = None

    @property
    def configured(self) -> bool:
        return bool(self.name or self.address)

    def matches(self, device: BLEDevice) -> bool:
        return bool(
            (self.address and device.address == self.address)
            or (self.name and device.name == self.name),
        )


_RECORDER_SENSOR_KINDS = tuple(RecorderSensorKind)


def _dispatch_direct(callback: Callable[..., object], *args: object) -> object:
    return callback(*args)


class Recorder:
    """Discover BLE sensors, normalize samples, and persist recording data."""

    def __init__(
        self,
        profile: SensorProfile,
        weight_kg: float | None,
        sport_type: SportTypesEnum,
        database: RecordingStore,
        on_error: Callable[[str], None],
        *,
        on_sample_update: Callable[
            [CyclingSample | HeartRateSample | RunningSample | TrainerSample],
            None,
        ]
        | None = None,
        test_mode: bool = False,
        dispatch: Callable[..., object] | None = None,
    ) -> None:
        logger.debug(f"Initializing Recorder with sport_type {sport_type}")
        logger.debug(f"HR sensor: name={profile.hr_name}, address={profile.hr_address}")
        logger.debug(f"Speed sensor: name={profile.speed_name}, address={profile.speed_address}")
        logger.debug(
            f"Cadence sensor: name={profile.cadence_name}, address={profile.cadence_address}",
        )
        logger.debug(f"Power sensor: name={profile.power_name}, address={profile.power_address}")
        logger.debug(
            f"Trainer sensor: name={profile.trainer_name}, address={profile.trainer_address}, "
            f"machine_type={profile.trainer_machine_type}",
        )

        self._ble_lock = asyncio.Lock()  # Lock for BLE operations
        self._thread: threading.Thread | None = None
        self._stop_requested = threading.Event()

        # Disable write when in test mode
        self.test_mode = bool(test_mode)

        self.weight_kg = weight_kg
        self.sport_type = sport_type
        self.profile = profile
        self.on_sample = on_sample_update
        self.on_error = on_error
        self._dispatch = dispatch or _dispatch_direct
        self.store = database
        self.loop = asyncio.new_event_loop()
        self._stop_event = asyncio.Event()
        self._recording = False
        self._recording_lock = threading.Lock()
        self._recording_generation = 0
        self._finalizing_activity_id: int | None = None
        self._finalization_in_progress = False
        self._finalization_done = threading.Event()
        self._finalization_done.set()
        self._shutdown_finalized_activity_id: int | None = None
        self._last_finalized_activity_id: int | None = None
        self._finalization_reconciled = False
        self.activity_id = None

        self._configured_sensors = {
            RecorderSensorKind.HEART_RATE: _ConfiguredSensor(
                name=profile.hr_name,
                address=profile.hr_address,
            ),
            RecorderSensorKind.SPEED: _ConfiguredSensor(
                name=profile.speed_name,
                address=profile.speed_address,
            ),
            RecorderSensorKind.CADENCE: _ConfiguredSensor(
                name=profile.cadence_name,
                address=profile.cadence_address,
            ),
            RecorderSensorKind.POWER: _ConfiguredSensor(
                name=profile.power_name,
                address=profile.power_address,
            ),
            RecorderSensorKind.TRAINER: _ConfiguredSensor(
                name=profile.trainer_name,
                address=profile.trainer_address,
            ),
        }

        self.trainer = TrainerController(
            sport_type=sport_type,
            trainer_supplied_hr=profile.trainer_supplied_hr,
            loop=self.loop,
            on_error=self._on_ble_error,
            test_mode=self.test_mode,
        )

        # Sample normalization is stateful for one recording session.
        self._sample_processor = SampleProcessor(weight_kg=weight_kg)

        # Connection status
        self.hr_connected = False
        self.speed_connected = False
        self.cadence_connected = False
        self.power_connected = False
        self.distance_connected = False

        # BLE muxes (only created if corresponding sensors are configured)
        self._speed_mux: RunningMux | CyclingMux | None = None
        self._hr_mux: HeartRateMux | None = None

        # BLE Discover devices
        self.devices: list[BLEDevice] = []

    def start(self) -> None:
        """Start the background BLE worker when it is not already running."""
        if self._thread and self._thread.is_alive():
            return
        if self.loop.is_closed():
            msg = "Cannot restart Recorder after its event loop has closed"
            raise RuntimeError(msg)

        self._stop_requested.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def shutdown(self, timeout: float = 15.0) -> bool:
        """Stop recording, shut down BLE resources, and wait for the worker."""
        logger.debug("Recorder.shutdown called")

        # Stop recording on shutdown
        with self._recording_lock:
            self._shutdown_finalized_activity_id = None
        deadline = time.monotonic() + timeout
        claim = self.begin_finalization()
        finalized_activity_id: int | None = None
        if claim.status is FinalizationStatus.STARTED and claim.activity_id is not None:
            finalized_activity_id = self.finish_finalization(claim.activity_id)
        elif claim.status is FinalizationStatus.ALREADY_RUNNING:
            self._finalization_done.wait(timeout=max(0.0, deadline - time.monotonic()))
            with self._recording_lock:
                if self._finalizing_activity_id is None:
                    finalized_activity_id = claim.activity_id
        elif claim.status is FinalizationStatus.NOT_RECORDING:
            with self._recording_lock:
                finalized_activity_id = self._last_finalized_activity_id
        with self._recording_lock:
            self._shutdown_finalized_activity_id = finalized_activity_id
        self._stop_requested.set()
        self.trainer.shutdown()

        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self._stop_event.set)
        elif self._thread is None:
            # Test-mode recorders never start their worker thread, so their
            # otherwise-unused loop belongs to this thread and can close here.
            if not self.loop.is_closed():
                self.loop.close()
            return True

        if self._thread:
            self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if self._thread.is_alive():
                logger.error(f"Recorder worker did not stop within {timeout:.1f}s")
                return False

        return True

    def take_shutdown_finalized_activity_id(self) -> int | None:
        """Consume the activity finalized by the most recent shutdown call."""
        with self._recording_lock:
            activity_id = self._shutdown_finalized_activity_id
            self._shutdown_finalized_activity_id = None
            return activity_id

    def start_recording(self) -> None:
        """Begin a new recording generation and reset processor state."""
        with self._recording_lock:
            if self._recording:
                return
            if self._finalizing_activity_id is not None:
                message = "Cannot start recording while activity finalization is pending"
                raise RuntimeError(message)
            self.activity_id = None
            # Only create an activity when not in test mode
            if not self.test_mode:
                self.activity_id = self.store.start_activity(sport_type=self.sport_type)
            else:
                self.activity_id = None
            self._recording = True
            self._recording_generation += 1
            self._sample_processor.reset()
        self._schedule_reset_distance()

    def begin_finalization(self) -> FinalizationClaim:
        """Claim the active activity for finalization without doing storage work."""
        with self._recording_lock:
            if not self._recording and self._finalizing_activity_id is None:
                self.activity_id = None
                return FinalizationClaim(FinalizationStatus.NOT_RECORDING)
            if self._finalizing_activity_id is None:
                self._recording = False
                self._recording_generation += 1
                self._finalizing_activity_id = self.activity_id

            activity_id = self._finalizing_activity_id
            if activity_id is None:
                self.activity_id = None
                return FinalizationClaim(FinalizationStatus.NO_ACTIVITY)
            if self._finalization_in_progress:
                return FinalizationClaim(FinalizationStatus.ALREADY_RUNNING, activity_id)
            self._finalization_in_progress = True
            self._finalization_done.clear()
            return FinalizationClaim(FinalizationStatus.STARTED, activity_id)

    def finish_finalization(self, activity_id: int) -> int | None:
        """Run storage finalization and clear state when it succeeds."""
        succeeded = False
        try:
            self.store.finalize_activity(activity_id)
            succeeded = True
        except Exception:
            logger.exception("Failed to finalize activity {}", activity_id)

        with self._recording_lock:
            self._finalization_in_progress = False
            if succeeded and self._finalizing_activity_id == activity_id:
                self._last_finalized_activity_id = activity_id
                self._finalization_reconciled = False
                self._finalizing_activity_id = None
                self.activity_id = None
            self._finalization_done.set()

        return activity_id if succeeded else None

    def claim_finalization_reconciliation(self, activity_id: int) -> bool:
        """Claim the one history update belonging to a finalized activity."""
        with self._recording_lock:
            if self._last_finalized_activity_id != activity_id or self._finalization_reconciled:
                return False
            self._finalization_reconciled = True
            return True

    def abort_finalization(self, activity_id: int) -> None:
        """Release a finalization claim when its background job was not submitted."""
        with self._recording_lock:
            if self._finalizing_activity_id == activity_id and self._finalization_in_progress:
                self._finalization_in_progress = False
                self._finalization_done.set()

    def stop_recording(self) -> int | None:
        """Finalize the active activity synchronously, retaining failed work for retry."""
        claim = self.begin_finalization()
        if claim.status is not FinalizationStatus.STARTED or claim.activity_id is None:
            return None
        return self.finish_finalization(claim.activity_id)

    @property
    def finalization_pending(self) -> bool:
        """Return whether the last activity still needs finalization."""
        with self._recording_lock:
            return self._finalizing_activity_id is not None

    @property
    def finalization_in_progress(self) -> bool:
        """Return whether a finalization worker currently owns the activity."""
        with self._recording_lock:
            return self._finalization_in_progress

    def _persist_if_recording(
        self,
        generation: int,
        persist: Callable[[int], None],
    ) -> None:
        """Persist only if the sample belongs to the current recording."""
        with self._recording_lock:
            if (
                self._recording
                and generation == self._recording_generation
                and self.activity_id is not None
            ):
                persist(self.activity_id)

    def _dispatch_sample(
        self,
        sample: CyclingSample | HeartRateSample | RunningSample | TrainerSample,
    ) -> None:
        """Dispatch a sample update without making persistence depend on it."""
        if self.on_sample is None:
            return
        try:
            self._dispatch(self.on_sample, sample)
        except Exception:
            logger.exception("Sample UI dispatch failed")

    @property
    def trainer_connected(self) -> bool:
        """Return whether the trainer transport is connected."""
        return self.trainer.trainer_mux is not None

    @property
    def trainer_configured(self) -> bool:
        """Return whether this profile includes a configured trainer."""
        return self._configured_sensors[RecorderSensorKind.TRAINER].configured

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._workflow())
        except Exception as e:
            logger.exception(f"Recorder workflow failed: {e}")
        finally:
            try:
                self.loop.run_until_complete(self._shutdown_loop())
            finally:
                asyncio.set_event_loop(None)
                self.loop.close()

    async def _shutdown_loop(self) -> None:
        """Cancel tasks left outside the main workflow before closing the loop."""
        current = asyncio.current_task()
        pending = [task for task in asyncio.all_tasks() if task is not current and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await self.loop.shutdown_asyncgens()

    async def _wait_for_stop(self) -> None:
        """Wait for shutdown without relying only on a cross-thread loop wakeup."""
        while not self._stop_requested.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=0.5)
            except TimeoutError:
                pass
            else:
                return

    # --- HR handling ---
    def _handle_hr_sample(
        self,
        sample: HeartRateSample,
        *,
        expected_generation: int | None = None,
    ) -> None:
        """Handle a HeartRateSample from HeartRateMux."""
        if sample.heart_rate_bpm is None:
            return

        logger.bind(data=sample).trace("Handling heart rate sample")
        with self._recording_lock:
            generation = self._recording_generation
            if expected_generation is not None and expected_generation != generation:
                return
            cleaned_sample, normalized = self._sample_processor.clean_heart_rate(sample)
        self._dispatch_sample(cleaned_sample)

        logger.bind(data=cleaned_sample).trace("Processed heart rate sample")

        # Persist to the DB if recording
        self._persist_if_recording(
            generation,
            lambda activity_id: self.store.insert_heart_rate(
                activity_id,
                cleaned_sample.timestamp_ms,
                normalized.bpm,
                normalized.rr_interval_ms,
            ),
        )

    # --- Running handling ---
    def _handle_running_sample(self, sample: RunningSample) -> None:
        logger.bind(data=sample).trace("Handling running sample")
        with self._recording_lock:
            generation = self._recording_generation
            cleaned_sample = self._sample_processor.process_running(
                sample,
                trainer_connected=self.trainer_connected,
            )

        self._dispatch_sample(cleaned_sample)

        logger.bind(data=cleaned_sample).trace("Processed running sample")

        # Persist to DB if recording
        self._persist_if_recording(
            generation,
            lambda activity_id: self.store.insert_running_metrics(
                activity_id,
                cleaned_sample,
                incline_percent=self.incline_percent,
            ),
        )

    def _handle_cycling_sample(self, sample: CyclingSample) -> None:
        logger.bind(data=sample).trace("Handling cycling sample")
        with self._recording_lock:
            generation = self._recording_generation
            cleaned_sample = self._sample_processor.process_cycling(sample)

        self._dispatch_sample(cleaned_sample)

        logger.bind(data=cleaned_sample).trace("Processed cycling sample")

        # Persist to DB if recording
        self._persist_if_recording(
            generation,
            lambda activity_id: self.store.insert_cycling_metrics(
                activity_id,
                cleaned_sample,
                incline_percent=self.incline_percent,
            ),
        )

    def _handle_trainer_sample(self, sample: TrainerSample) -> None:
        logger.bind(data=sample).trace("Handling trainer sample")
        with self._recording_lock:
            generation = self._recording_generation
            cleaned_sample = self._sample_processor.process_trainer(sample)
            self.trainer.handle_sample(sample)
            has_embedded_heart_rate = self.trainer.handle_embedded_heart_rate(
                sample.heart_rate_bpm,
            )

        if has_embedded_heart_rate:
            self.hr_connected = True
            self._handle_hr_sample(
                HeartRateSample(
                    timestamp_ms=sample.timestamp_ms,
                    heart_rate_bpm=sample.heart_rate_bpm,
                ),
                expected_generation=generation,
            )

        # Update UI
        self._dispatch_sample(cleaned_sample)

        logger.bind(data=cleaned_sample).trace("Processed trainer sample")

        # Persist to DB if recording
        def persist_trainer_sample(activity_id: int) -> None:
            if self.sport_type == SportTypesEnum.biking:
                self.store.insert_cycling_metrics(
                    activity_id,
                    cleaned_sample,
                    incline_percent=self.incline_percent,
                )
            elif self.sport_type == SportTypesEnum.running:
                self.store.insert_running_metrics(
                    activity_id,
                    cleaned_sample,
                    incline_percent=self.incline_percent,
                )
            else:
                logger.error(f"Unknown sport type {self.sport_type} for trainer sample insertion")

        self._persist_if_recording(generation, persist_trainer_sample)

    def _match_discovered_devices(self) -> None:
        """Attach discovered BLE devices to the matching configured roles."""
        for device in self.devices:
            for kind in _RECORDER_SENSOR_KINDS:
                sensor = self._configured_sensors[kind]
                if sensor.matches(device):
                    logger.debug(
                        f"Matched {kind.value} device from scan: {device.address} ({device.name})",
                    )
                    sensor.device = device

    def _start_matched_device_loops(self) -> list[asyncio.Task[None]]:
        """Start loops for sensors found during the initial scan."""
        tasks: list[asyncio.Task[None]] = []
        hr = self._configured_sensors[RecorderSensorKind.HEART_RATE]
        speed = self._configured_sensors[RecorderSensorKind.SPEED]
        cadence = self._configured_sensors[RecorderSensorKind.CADENCE]
        power = self._configured_sensors[RecorderSensorKind.POWER]
        trainer = self._configured_sensors[RecorderSensorKind.TRAINER]

        if hr.device:
            logger.debug(
                f"Starting HR loop with device {hr.device.address} ({hr.device.name})",
            )
            tasks.append(self.loop.create_task(self._hr_loop()))
        if speed.device or cadence.device or power.device:
            logger.debug(
                "Starting speed loop with devices: speed={} ({}), cadence={} ({}), power={} ({})",
                speed.device.address if speed.device else "none",
                speed.device.name if speed.device else "none",
                cadence.device.address if cadence.device else "none",
                cadence.device.name if cadence.device else "none",
                power.device.address if power.device else "none",
                power.device.name if power.device else "none",
            )
            tasks.append(self.loop.create_task(self._speed_loop()))
        if trainer.device:
            logger.debug(
                f"Starting trainer loop with device {trainer.device.address} "
                f"({trainer.device.name})",
            )
            tasks.append(self.loop.create_task(self._trainer_loop()))
        return tasks

    def _start_unmatched_device_loops(self) -> list[asyncio.Task[None]]:
        """Start loops for configured sensors absent from the initial scan."""
        tasks: list[asyncio.Task[None]] = []
        hr = self._configured_sensors[RecorderSensorKind.HEART_RATE]
        speed = self._configured_sensors[RecorderSensorKind.SPEED]
        cadence = self._configured_sensors[RecorderSensorKind.CADENCE]
        power = self._configured_sensors[RecorderSensorKind.POWER]
        trainer = self._configured_sensors[RecorderSensorKind.TRAINER]

        if hr.configured and not hr.device:
            logger.debug("Starting HR loop without matched device (will wait for connection)")
            tasks.append(self.loop.create_task(self._hr_loop()))
        if any(sensor.configured for sensor in (speed, cadence, power)) and not any(
            sensor.device for sensor in (speed, cadence, power)
        ):
            logger.debug("Starting speed loop without matched devices (will wait for connections)")
            tasks.append(self.loop.create_task(self._speed_loop()))
        if trainer.configured and not trainer.device:
            logger.debug("Starting trainer loop without matched device (will wait for connection)")
            tasks.append(self.loop.create_task(self._trainer_loop()))
        return tasks

    def _start_device_loops(self) -> list[asyncio.Task[None]]:
        """Start all sensor loops, including loops waiting for late connections."""
        return self._start_matched_device_loops() + self._start_unmatched_device_loops()

    async def _stop_device_loops(self, device_tasks: list[asyncio.Task[None]]) -> None:
        """Cancel sensor loops and allow each mux to clean up."""
        logger.debug(f"Stop event received, cancelling {len(device_tasks)} device tasks")
        for task in device_tasks:
            task.cancel()

        for task in device_tasks:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
                logger.debug(f"Task {task.get_name()} finished cleanly")
            except TimeoutError:
                logger.warning(f"Task {task.get_name()} did not finish within 10s after cancel")
            except asyncio.CancelledError:
                logger.debug(f"Task {task.get_name()} cancelled")
            except Exception as error:
                logger.warning(f"Task {task.get_name()} raised on shutdown: {error}")

    async def _workflow(self) -> None:
        logger.debug("Starting Recorder workflow")
        # Bind the stop event to the Recorder's worker loop.
        self._stop_event = asyncio.Event()
        if self._stop_requested.is_set():
            self._stop_event.set()

        # Scan for BLE devices upfront, call bleaksport with found devices to speed up connection
        self.devices = await BleakScanner.discover(
            timeout=5.0,
        )

        logger.debug(f"BLE scan complete, found {len(self.devices)} devices")
        logger.bind(data=self.devices).trace("Discovered BLE devices")

        logger.debug("Matching configured devices to scan results")
        self._match_discovered_devices()

        if not any(sensor.device for sensor in self._configured_sensors.values()):
            logger.debug(
                "No configured devices found in BLE scan results,"
                " starting loops without matched devices and wait for connections",
            )
        else:
            logger.debug(
                "Device matching complete, starting loops for found devices",
            )

        device_tasks = self._start_device_loops()

        if not device_tasks:
            logger.warning("No device loops started — check configuration and BLE availability")
            return

        # Wait for explicit stop only — let each mux's internal loop handle reconnects
        await self._wait_for_stop()
        await self._stop_device_loops(device_tasks)

        logger.debug("Workflow exiting")

    async def _speed_loop(self) -> None:
        speed = self._configured_sensors[RecorderSensorKind.SPEED]
        cadence = self._configured_sensors[RecorderSensorKind.CADENCE]
        power = self._configured_sensors[RecorderSensorKind.POWER]
        if self.sport_type == SportTypesEnum.running:
            mux = RunningMux(
                speed_addr=speed.device or speed.address,
                cadence_addr=cadence.device or cadence.address,
                power_addr=power.device or power.address,
                on_sample=self._handle_running_sample,
                on_status=self._on_ble_error,
                on_link=self._on_running_link,
                ble_lock=self._ble_lock,
            )
        elif self.sport_type == SportTypesEnum.biking:
            csc_addr = speed.device or cadence.device or speed.address or cadence.address
            mux = CyclingMux(
                csc_addr=csc_addr,
                cps_addr=power.device or power.address,
                on_sample=self._handle_cycling_sample,
                on_status=self._on_ble_error,
                on_link=self._on_running_link,  # same link handler for cycling mux
                ble_lock=self._ble_lock,
            )
        else:
            logger.error(f"Unknown sport type {self.sport_type} for speed loop")
            return
        self._speed_mux = mux
        try:
            await mux.start()
        finally:
            logger.debug("Cleaning up speed connections")
            await mux.stop()
            self._speed_mux = None

    def _on_running_link(
        self,
        _addr: str,
        connected: bool,  # noqa: FBT001 - BLE link callback supplies positional state
        roles: dict[str, bool],
    ) -> None:
        # RSCS drives both speed & cadence cards
        self.speed_connected = connected and roles.get("rsc", False)
        self.cadence_connected = connected and roles.get("rsc", False)
        self.distance_connected = connected and roles.get("rsc", False)
        self.power_connected = connected and roles.get("cps", False)

    async def _trainer_loop(self) -> None:
        trainer = self._configured_sensors[RecorderSensorKind.TRAINER]
        mux = TrainerMux(
            addr=trainer.device or trainer.address,
            machine_type=(
                MachineType(self.profile.trainer_machine_type)
                if self.profile.trainer_machine_type is not None
                else None
            ),
            on_sample=self._handle_trainer_sample,
            on_status=self._on_ble_error,
            on_link=self._on_trainer_link,
        )
        self.trainer.trainer_mux = mux
        try:
            await mux.start()
        finally:
            logger.debug("Cleaning up trainer connections")
            with contextlib.suppress(Exception):
                await mux.stop()
            self.trainer.trainer_mux = None

    def _on_trainer_link(
        self,
        _addr: str,
        connected: bool,  # noqa: FBT001 - BLE link callback supplies positional state
        _info: dict[str, bool],
    ) -> None:
        self.speed_connected = connected
        self.cadence_connected = connected
        self.power_connected = connected
        self.distance_connected = connected

        if not connected:
            self.trainer.on_link(connected=False)
            if self.profile.trainer_supplied_hr:
                self.hr_connected = False
        else:
            self.trainer.on_link(connected=True)

    async def _hr_loop(self) -> None:
        """Connect to the HR monitor via HeartRateMux and stream samples."""
        heart_rate = self._configured_sensors[RecorderSensorKind.HEART_RATE]
        self._hr_mux = HeartRateMux(
            addr=heart_rate.device or heart_rate.address,
            name=heart_rate.name,
            on_sample=self._handle_hr_sample,
            on_status=self._on_ble_error,
            on_link=self._on_hr_link,
            ble_lock=self._ble_lock,
        )
        try:
            await self._hr_mux.start()
        finally:
            logger.debug("Cleaning up hr connections")
            with contextlib.suppress(Exception):
                await self._hr_mux.stop()
            self._hr_mux = None

    def _on_hr_link(
        self,
        _addr: str,
        connected: bool,  # noqa: FBT001 - BLE link callback supplies positional state
        _roles: dict[str, bool],
    ) -> None:
        self.hr_connected = connected

    def _on_ble_error(self, msg: str) -> None:
        self._dispatch(self.on_error, msg)

    def _schedule_reset_distance(self) -> None:
        """Kick an async reset in the BLE loop without blocking the UI."""
        if not self.loop.is_running():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._reset_distance_workflow(), self.loop)
        except Exception as e:
            self._on_ble_error(f"Failed to schedule distance reset: {e}")

    def set_target_power(self, watts: int) -> None:
        """Set target power on the trainer if supported."""
        self._sync_trainer_loop()
        self.trainer.set_target_power(watts)

    def set_target_resistance(self, resistance: float) -> None:
        """Set target resistance on the trainer if supported."""
        self._sync_trainer_loop()
        self.trainer.set_target_resistance(resistance)

    def set_target_speed(self, speed_kmh: float) -> None:
        """Set target treadmill speed in kilometers per hour."""
        self._sync_trainer_loop()
        self.trainer.set_target_speed(speed_kmh)

    def set_target_heart_rate(self, bpm: int) -> bool:
        """Set trainer-controlled target HR when its own HR telemetry is available."""
        self._sync_trainer_loop()
        return self.trainer.set_target_heart_rate(bpm)

    def neutralize_trainer(self) -> None:
        """Apply a fail-safe zero trainer target when a session stops."""
        self._sync_trainer_loop()
        self.trainer.neutralize()

    @property
    def trainer_heart_rate_control_available(self) -> bool:
        """Return whether trainer-controlled HR targets are currently safe to use."""
        return self.trainer.trainer_heart_rate_control_available

    def _sync_trainer_loop(self) -> None:
        self.trainer.loop = self.loop

    async def _reset_distance_workflow(self, *, wait_s: float = 6.0) -> None:
        """
        Wait up to wait_s for RSCS to be connected, then try SC Control Point reset.
        Fall back silently (baseline subtraction will handle it).
        """
        # Wait a little for the RSCS link to come up
        t0 = self.loop.time()
        while (self.loop.time() - t0) < wait_s and not self._stop_event.is_set():
            if self.speed_connected and self._speed_mux:
                break
            await asyncio.sleep(0.2)

        mux = self._speed_mux
        if not mux:
            return
        if not isinstance(mux, RunningMux):
            logger.debug("Cycling speed sensor does not support distance reset; using baseline")
            return

        try:
            ok = await mux.reset_distance()
            if ok:
                # Optional: set baseline to 0 so first sample shows exactly 0.00 mi.
                self._sample_processor.set_distance_baseline(0.0)
            else:
                # Not supported / timed out — baseline logic will take over
                logger.warning("Sensor didn't accept distance reset; using baseline")
        except Exception as e:
            # Don't fail the session; just fall back
            logger.error(f"SC Control Point reset failed: {e}")

    @property
    def incline_percent(self) -> float | None:
        """Return the current incline used by the sample processor."""
        return self._sample_processor.incline_percent

    def set_incline(self, percent: float | None) -> None:
        """Set the current incline percentage (None = flat / unknown)."""
        self._sample_processor.set_incline(percent)

    # --- Test-mode injection ---
    def inject_test_sample(
        self,
        sample: CyclingSample | RunningSample | TrainerSample | HeartRateSample,
    ) -> None:
        """
        Directly inject a pre-built sample into the recorder,
        bypassing BLE. Used exclusively in test_mode to exercise the full recorder pipeline
        (distance baseline, altitude accumulation, DB writes, UI callbacks) from
        simulated data produced by the UI layer.

        Safe to call from the GTK main thread — the handlers only touch recorder state
        and schedule dispatch callbacks; no asyncio involvement is needed.
        """
        if not self.test_mode:
            logger.warning("inject_test_sample called outside of test_mode — ignoring")
            return

        if isinstance(sample, TrainerSample):
            self._handle_trainer_sample(sample)
        elif isinstance(sample, RunningSample):
            self._handle_running_sample(sample)
        elif isinstance(sample, CyclingSample):
            self._handle_cycling_sample(sample)
        elif isinstance(sample, HeartRateSample):
            self._handle_hr_sample(sample)
        else:
            logger.error(f"inject_test_sample: unrecognised sample type {type(sample)}")

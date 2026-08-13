from bleaksport import CyclingSample, HeartRateSample, RunningSample, TrainerSample
from fitness_tracker.hardware.processor import SampleProcessor

_EXPECTED_ELAPSED_MS = 1_000
_EXPECTED_TWO_SECOND_TIMESTAMP_MS = 2_000
_EXPECTED_ESTIMATED_POWER_WATTS = 207
_EXPECTED_BASE_POWER_WATTS = 100
_EXPECTED_DISTANCE_DELTA_M = 10.0
_EXPECTED_TRAINER_DISTANCE_DELTA_M = 3.0
_EXPECTED_ALTITUDE_M = 0.8
_EXPECTED_RR_INTERVAL_MS = 420.0


def test_running_samples_rebase_distance_accumulate_altitude_and_estimate_power() -> None:
    processor = SampleProcessor(weight_kg=70.0)
    processor.set_incline(8.0)

    first = processor.process_running(
        RunningSample(
            timestamp_ms=1_000,
            distance_m=100.0,
            power_watts=100,
            speed_mps=3.0,
        ),
        trainer_connected=False,
    )
    second = processor.process_running(
        RunningSample(
            timestamp_ms=2_000,
            distance_m=110.0,
            power_watts=100,
            speed_mps=3.0,
        ),
        trainer_connected=False,
    )

    assert first.timestamp_ms == 0
    assert first.distance_m == 0.0
    assert first.power_watts == _EXPECTED_ESTIMATED_POWER_WATTS
    assert first.altitude_m == 0.0
    assert second.timestamp_ms == _EXPECTED_ELAPSED_MS
    assert second.distance_m == _EXPECTED_DISTANCE_DELTA_M
    assert second.power_watts == _EXPECTED_ESTIMATED_POWER_WATTS
    assert second.altitude_m == _EXPECTED_ALTITUDE_M


def test_running_power_is_not_estimated_for_trainer_samples() -> None:
    processor = SampleProcessor(weight_kg=70.0)
    processor.set_incline(8.0)

    sample = processor.process_running(
        RunningSample(
            timestamp_ms=1_000,
            distance_m=100.0,
            power_watts=100,
            speed_mps=3.0,
        ),
        trainer_connected=True,
    )

    assert sample.power_watts == _EXPECTED_BASE_POWER_WATTS


def test_cycling_and_trainer_samples_rebase_session_values() -> None:
    processor = SampleProcessor()
    processor.set_incline(5.0)

    cycling = processor.process_cycling(
        CyclingSample(timestamp_ms=2_000, distance_m=25.0),
    )
    assert cycling.timestamp_ms == 0
    assert cycling.distance_m == 0.0
    assert cycling.altitude_m == 0.0

    processor.reset()
    trainer = processor.process_trainer(
        TrainerSample(timestamp_ms=3_000, distance_m=12.0),
    )
    later = processor.process_trainer(
        TrainerSample(timestamp_ms=4_000, distance_m=15.0),
    )
    assert trainer.timestamp_ms == 0
    assert trainer.distance_m == 0.0
    assert later.timestamp_ms == _EXPECTED_ELAPSED_MS
    assert later.distance_m == _EXPECTED_TRAINER_DISTANCE_DELTA_M


def test_heart_rate_cleaning_rebases_and_smooths_for_ui_and_persistence() -> None:
    processor = SampleProcessor()
    cleaned = [
        processor.clean_heart_rate(
            HeartRateSample(timestamp_ms=timestamp, heart_rate_bpm=bpm, rr_interval_ms=420.0),
        )
        for timestamp, bpm in ((1_000, 150), (2_000, 140), (3_000, 145))
    ]

    assert [sample.timestamp_ms for sample, _normalized in cleaned] == [
        0,
        _EXPECTED_ELAPSED_MS,
        _EXPECTED_TWO_SECOND_TIMESTAMP_MS,
    ]
    assert [sample.heart_rate_bpm for sample, _normalized in cleaned] == [150, 145, 145]
    assert [normalized.bpm for _sample, normalized in cleaned] == [150, 145, 145]
    assert all(
        normalized.rr_interval_ms == _EXPECTED_RR_INTERVAL_MS for _sample, normalized in cleaned
    )

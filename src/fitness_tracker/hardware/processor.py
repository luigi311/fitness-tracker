"""Normalize raw BLE samples before they cross into persistence."""

from collections import deque
from statistics import median

from bleaksport.models import (
    CyclingSample,
    HeartRateSample,
    RunningSample,
    TrainerSample,
)

from fitness_tracker.core.measurements import NormalizedHeartRate


class SampleProcessor:
    """Clean BLE measurements without depending on the transport or database."""

    def __init__(self, weight_kg: float | None = None) -> None:
        self.weight_kg = weight_kg
        self.incline_percent: float | None = None
        self._bpm_history: deque[int] = deque(maxlen=3)
        self.reset()

    def reset(self) -> None:
        """Reset state that belongs to one recording session."""
        self._start_ms: int | None = None
        self._dist0_m: float | None = None
        self._current_altitude_m = 0.0
        self._last_distance_m: float | None = None
        self._bpm_history.clear()

    def set_incline(self, percent: float | None) -> None:
        """Set the current incline used for running power normalization."""
        self.incline_percent = percent

    def set_distance_baseline(self, distance_m: float | None) -> None:
        """Set the external distance baseline after a sensor reset."""
        self._dist0_m = distance_m

    def process_heart_rate(self, sample: HeartRateSample) -> NormalizedHeartRate:
        """Return a normalized heart-rate sample from an upstream BLE model."""
        if sample.heart_rate_bpm is None:
            message = "heart-rate samples must contain a BPM value"
            raise ValueError(message)

        return NormalizedHeartRate(
            timestamp_ms=sample.timestamp_ms,
            bpm=sample.heart_rate_bpm,
            rr_interval_ms=sample.rr_interval_ms,
        )

    def clean_heart_rate(
        self,
        sample: HeartRateSample,
    ) -> tuple[HeartRateSample, NormalizedHeartRate]:
        """Return the UI sample and persistence value for one heart-rate frame."""
        normalized = self.process_heart_rate(sample)
        bpm = normalized.bpm
        delta_ms = self._relative_timestamp(sample.timestamp_ms)
        self._bpm_history.append(bpm)
        smoothed_bpm = int(median(self._bpm_history))
        return (
            HeartRateSample(timestamp_ms=delta_ms, heart_rate_bpm=smoothed_bpm),
            normalized.model_copy(update={"bpm": smoothed_bpm}),
        )

    def process_running(
        self,
        sample: RunningSample,
        *,
        trainer_connected: bool,
    ) -> RunningSample:
        """Normalize a running frame, including distance and incline power."""
        delta_ms = self._relative_timestamp(sample.timestamp_ms)
        watts = sample.power_watts
        adjusted_distance_m = self._adjust_distance(sample.distance_m)
        altitude_m = self._accumulate_altitude(sample.distance_m)

        if not trainer_connected and watts and self.weight_kg and self.incline_percent:
            # Estimate additional power from incline for footpods. The formula is
            # derived from the QZ reference and Stryd calibration data.
            speed_kmh = sample.speed_kph or 0.0
            speed_term = (-0.96 + 1.33 * speed_kmh) * self.incline_percent
            watts = max(round(watts + speed_term), 0)

        return sample.model_copy(
            update={
                "timestamp_ms": delta_ms,
                "distance_m": adjusted_distance_m,
                "power_watts": watts,
                "altitude_m": altitude_m,
            },
        )

    def process_cycling(self, sample: CyclingSample) -> CyclingSample:
        """Normalize a cycling frame, including distance and altitude."""
        delta_ms = self._relative_timestamp(sample.timestamp_ms)
        adjusted_distance_m = self._adjust_distance(sample.distance_m)
        altitude_m = self._accumulate_altitude(sample.distance_m)
        return sample.model_copy(
            update={
                "timestamp_ms": delta_ms,
                "distance_m": adjusted_distance_m,
                "altitude_m": altitude_m,
            },
        )

    def process_trainer(self, sample: TrainerSample) -> TrainerSample:
        """Normalize a trainer frame's timestamp and distance."""
        return sample.model_copy(
            update={
                "timestamp_ms": self._relative_timestamp(sample.timestamp_ms),
                "distance_m": self._adjust_distance(sample.distance_m),
            },
        )

    def _relative_timestamp(self, timestamp_ms: int) -> int:
        if self._start_ms is None:
            self._start_ms = timestamp_ms
        return int(timestamp_ms - self._start_ms)

    def _adjust_distance(self, distance_m: float | None) -> float | None:
        if self._dist0_m is None and distance_m is not None:
            self._dist0_m = distance_m
        if distance_m is None or self._dist0_m is None:
            return distance_m
        return max(0.0, distance_m - self._dist0_m)

    def _accumulate_altitude(self, distance_m: float | None) -> float:
        if distance_m is None or self.incline_percent is None:
            return self._current_altitude_m

        if self._last_distance_m is not None:
            delta = max(0.0, distance_m - self._last_distance_m)
            self._current_altitude_m += delta * (self.incline_percent / 100.0)

        self._last_distance_m = distance_m
        return self._current_altitude_m

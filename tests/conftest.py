"""Shared test doubles and fixtures."""

from typing import Protocol

import pytest
from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.hardware.location import (
    LocationFilter,
    LocationFix,
    LocationFixCallback,
    LocationPolicy,
    LocationState,
    LocationStateCallback,
    max_speed_mps_for_sport,
)


class FakeLocationSource:
    """Deterministic source used by lifecycle tests instead of a real portal."""

    def __init__(self, sport_type: SportTypesEnum = SportTypesEnum.running) -> None:
        self.policy: LocationPolicy | None = None
        self.started = False
        self.start_count = 0
        self.stop_count = 0
        self._max_speed_mps = max_speed_mps_for_sport(sport_type)
        self._filter: LocationFilter | None = None
        self._on_fix: LocationFixCallback | None = None
        self._on_state: LocationStateCallback | None = None

    async def start(
        self,
        policy: LocationPolicy,
        on_fix: LocationFixCallback,
        on_state: LocationStateCallback,
    ) -> None:
        self.policy = policy
        self._filter = LocationFilter(policy, max_speed_mps=self._max_speed_mps)
        self._on_fix = on_fix
        self._on_state = on_state
        self.started = True
        self.start_count += 1
        on_state(LocationState.ACQUIRING, None)

    async def stop(self) -> None:
        self.started = False
        self.stop_count += 1
        self._filter = None
        self._on_fix = None
        self._on_state = None

    def emit_fix(self, fix: LocationFix, timestamp_ms: int) -> None:
        if not self.started or self._filter is None or self._on_fix is None:
            return
        accepted = self._filter.accept(fix, timestamp_ms)
        if accepted is not None:
            self._on_fix(accepted)

    def emit_state(self, state: LocationState, detail: str | None = None) -> None:
        if self.started and self._on_state is not None:
            self._on_state(state, detail)


class FakeLocationSourceFactory(Protocol):
    """Callable contract for creating isolated fake sources."""

    def __call__(
        self,
        sport_type: SportTypesEnum = SportTypesEnum.running,
    ) -> FakeLocationSource:
        """Create a source configured for one sport."""


@pytest.fixture
def fake_location_source() -> FakeLocationSourceFactory:
    """Return a factory for isolated sources with spike filtering enabled."""
    return lambda sport_type=SportTypesEnum.running: FakeLocationSource(sport_type)

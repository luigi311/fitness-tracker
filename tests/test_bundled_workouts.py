"""Check that the bundled tempo workouts provide guidance and can finish."""

import pytest
from fitness_tracker.core.guidance import TargetDomain, resolve_step_guidance
from fitness_tracker.core.settings import PersonalSettings
from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.core.units import UnitSystem
from fitness_tracker.core.zones import HeartRateZones
from fitness_tracker.workout_execution import WorkoutExecution
from fitness_tracker.workouts import format_workout_summary, load_test_workout
from workout_parser import RangeTarget


@pytest.mark.parametrize(
    ("sport_type", "domain"),
    [(SportTypesEnum.running, TargetDomain.PACE), (SportTypesEnum.biking, TargetDomain.POWER)],
)
def test_tempo_intervals_guidance_and_two_minute_completion(sport_type, domain):
    workout = load_test_workout(sport_type)
    steps = list(workout.expanded_steps())

    assert workout.name == "Tempo intervals"
    assert format_workout_summary(workout, UnitSystem.METRIC) == "2 min"
    assert [step.duration.seconds for step in steps] == [20, 20, 10, 20, 10, 20, 10, 10]
    assert [step.instruction for step in steps] == [
        "Warm up",
        "Tempo",
        "Recover",
        "Tempo",
        "Recover",
        "Tempo",
        "Recover",
        "Cool down",
    ]
    for step in steps:
        guidance = resolve_step_guidance(
            step,
            None,
            personal=PersonalSettings(),
            zones=HeartRateZones(resting_hr=60, max_hr=190).thresholds,
            unit_system=UnitSystem.METRIC,
        )
        assert guidance.domain is domain
    if sport_type is SportTypesEnum.running:
        assert steps[1].speed_mps == RangeTarget(low=1000 / 300, high=1000 / 285)
    else:
        assert steps[1].power_watts == RangeTarget(low=200, high=230)

    execution = WorkoutExecution(steps)
    seconds = 0
    for index, step in enumerate(steps):
        snapshot = execution.update(seconds, 0)
        assert snapshot.active_index == index
        assert snapshot.remaining_seconds == step.duration.seconds
        assert not snapshot.completed
        seconds += step.duration.seconds
    assert execution.update(120, 0).completed

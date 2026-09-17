"""Averages count actual sensor samples, independently for each metric."""

from fitness_tracker.core.metric_averages import MetricAverages


def test_independent_sensor_counts_and_missing_values() -> None:
    averages = MetricAverages()
    averages.observe(hr=120)
    averages.observe(hr=160)
    averages.observe(speed_mps=2, cadence=None, power_w=200)
    averages.observe(speed_mps=4, cadence=170, power_w=0)
    averages.observe(speed_mps=None, cadence=float("nan"), power_w=-1)
    averages.observe(hr=float("inf"))

    assert averages.values() == {
        "hr": 140,
        "speed_mps": 3,
        "cadence": 170,
        "power_w": 100,
    }
    assert MetricAverages().values() == {}

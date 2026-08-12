from fitness_tracker.core.throttle import TrainerTargetThrottle
from fitness_tracker.core.trainer_mode import TrainerMode


def test_power_requires_a_three_watt_change_and_two_seconds() -> None:
    throttle = TrainerTargetThrottle()

    assert throttle.should_send(TrainerMode.POWER, 150, 0.0)
    throttle.mark_sent(TrainerMode.POWER, 150, 0.0)

    assert not throttle.should_send(TrainerMode.POWER, 153, 2.0)
    assert not throttle.should_send(TrainerMode.POWER, 152, 2.1)
    assert throttle.should_send(TrainerMode.POWER, 153, 2.1)


def test_speed_requires_a_changed_value_and_two_seconds() -> None:
    throttle = TrainerTargetThrottle()
    throttle.mark_sent(TrainerMode.SPEED, 10.0, 0.0)

    assert not throttle.should_send(TrainerMode.SPEED, 10.0, 3.0)
    assert not throttle.should_send(TrainerMode.SPEED, 10.1, 2.0)
    assert throttle.should_send(TrainerMode.SPEED, 10.1, 2.1)


def test_heart_rate_sends_on_change_or_after_ten_seconds() -> None:
    throttle = TrainerTargetThrottle()
    throttle.mark_sent(TrainerMode.HEART_RATE, 150, 0.0)

    assert throttle.should_send(TrainerMode.HEART_RATE, 151, 0.1)
    assert not throttle.should_send(TrainerMode.HEART_RATE, 150, 9.9)
    assert throttle.should_send(TrainerMode.HEART_RATE, 150, 10.0)


def test_mode_changes_and_reset_send_immediately() -> None:
    throttle = TrainerTargetThrottle()
    throttle.mark_sent(TrainerMode.POWER, 150, 0.0)

    assert throttle.should_send(TrainerMode.SPEED, 10.0, 0.1)

    throttle.mark_sent(TrainerMode.SPEED, 10.0, 0.1)
    throttle.reset()
    assert throttle.should_send(TrainerMode.SPEED, 10.0, 0.2)

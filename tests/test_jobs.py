"""Tests for background-job cleanup boundaries."""

from collections.abc import Callable
from threading import Event

from fitness_tracker.services.jobs import BackgroundJobRunner


def test_shutdown_delivery_runs_discard_hook() -> None:
    queued: list[Callable[[], bool | None]] = []
    marshalled = Event()
    discarded = Event()

    def marshal(callback: Callable[[], bool | None]) -> None:
        queued.append(callback)
        marshalled.set()

    runner = BackgroundJobRunner(marshal)
    runner.submit(
        "completed-during-shutdown",
        lambda _token: "done",
        on_discard=discarded.set,
    )
    assert marshalled.wait(timeout=2.0)

    runner.shutdown()
    queued[0]()

    assert discarded.is_set()

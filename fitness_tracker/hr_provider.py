import asyncio
from typing import Callable


class HeartRateProvider:
    name: str = "Base"

    @staticmethod
    def matches(name: str) -> bool:
        return False

    @staticmethod
    async def connect_and_stream(
        device, frame_queue: asyncio.Queue, on_disconnect: Callable
    ):
        raise NotImplementedError


# Import actual implementations
from fitness_tracker.hr_polar import PolarProvider

AVAILABLE_PROVIDERS = [
    PolarProvider,
]

from __future__ import annotations
from typing import Iterable

class UploadProvider:
    """Base interface for pushing completed workouts to a service."""
    def upload_activities(self, act_ids: Iterable[int]) -> list[tuple[int, bool, str | None]]:
        """
        Returns [(activity_id, success_bool, error_message_or_None)].
        """
        raise NotImplementedError

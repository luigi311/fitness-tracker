"""Lifecycle states shared by session views and their controller."""

from enum import StrEnum


class SessionState(StrEnum):
    """State of a session page and its recording controls."""

    PREVIEW = "preview"
    RUNNING = "running"
    PAUSED = "paused"

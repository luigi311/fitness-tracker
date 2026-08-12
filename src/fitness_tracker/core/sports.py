"""Domain-level sport type identifiers."""

from enum import Enum


class SportTypesEnum(Enum):
    """Sport type IDs persisted by the database."""

    running = 1
    biking = 2
    unknown = 99

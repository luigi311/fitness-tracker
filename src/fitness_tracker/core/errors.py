"""Shared domain errors with user-actionable failure semantics."""

from __future__ import annotations


class UserActionableError(RuntimeError):
    """A routine failure that should be reported without an unexpected traceback."""


class DatabaseConnectionError(UserActionableError):
    """A remote database could not be reached."""


class DatabaseMigrationError(UserActionableError):
    """A database cannot be prepared safely for synchronization."""

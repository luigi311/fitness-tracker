"""Typed errors raised while crossing an external integration boundary."""

from __future__ import annotations

from fitness_tracker.core.errors import UserActionableError


class IntegrationError(UserActionableError):
    """Base error for failures reported by an external integration."""

    def __init__(self, service: str, message: str) -> None:
        self.service = service
        super().__init__(f"{service}: {message}")


class IntegrationTransportError(IntegrationError):
    """The integration could not complete a network request."""

    def __init__(self, service: str, message: str, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(service, message)


class IntegrationResponseError(IntegrationError):
    """The integration returned data that violates the expected response schema."""


class IntegrationConfigurationError(IntegrationError):
    """The integration could not start because its configuration is invalid."""

"""Typed Intervals.icu credentials, transport, and response models."""

from __future__ import annotations

from datetime import date, datetime  # noqa: TC003 - Pydantic resolves these at runtime
from typing import TYPE_CHECKING, Protocol

import requests
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from requests.auth import HTTPBasicAuth

from fitness_tracker.integrations.errors import (
    IntegrationResponseError,
    IntegrationTransportError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_API_BASE = "https://intervals.icu/api/v1"
_INTEGRATION_NAME = "intervals.icu"


class IntervalsICUCredentials(BaseModel):
    """Validated credentials required by every Intervals.icu operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    athlete_id: str = Field(min_length=1)
    api_key: str = Field(min_length=1)


class IcuWorkoutEvent(BaseModel):
    """Validated workout event returned by the Intervals.icu API."""

    # Keep unknown fields so the persisted wrapper retains the complete API
    # event for workout_parser and future application features.
    model_config = ConfigDict(extra="allow")

    type: str = ""
    name: str | None = None
    title: str | None = None
    workout_filename: str | None = None
    workout_file_base64: str | None = None
    start_date_local: datetime | None = None
    start_date: datetime | None = None

    @property
    def planned_date(self) -> date | None:
        """Return the local planned date from the first available timestamp."""
        dt = self.start_date_local or self.start_date
        return dt.date() if dt else None


class IcuUploadResponse(BaseModel):
    """Validated activity identifier returned after an Intervals.icu upload."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str | int | None = None
    activity_id: str | int | None = Field(default=None, alias="activityId")

    @classmethod
    def from_response(cls, response: requests.Response) -> IcuUploadResponse:
        """Parse a successful upload response and require its activity identifier."""
        try:
            payload = cls.model_validate(response.json())
        except (ValidationError, ValueError) as exc:
            message = "returned unexpected data"
            raise IntegrationResponseError(
                _INTEGRATION_NAME,
                message,
            ) from exc

        if payload.provider_id is None:
            message = "returned no activity identifier"
            raise IntegrationResponseError(
                _INTEGRATION_NAME,
                message,
            )
        return payload

    @property
    def provider_id(self) -> str | None:
        """Return the API's activity identifier in the database representation."""
        value = self.id if self.id is not None else self.activity_id
        if value is None:
            return None
        provider_id = str(value)
        return provider_id or None


class _HttpTransport(Protocol):
    def get(self, url: str, **kwargs: object) -> requests.Response: ...

    def post(self, url: str, **kwargs: object) -> requests.Response: ...


_ICU_WORKOUT_EVENTS = TypeAdapter(list[IcuWorkoutEvent])


class IntervalsICUClient:
    """Send authenticated Intervals.icu requests and validate their responses."""

    def __init__(
        self,
        credentials: IntervalsICUCredentials,
        *,
        transport: _HttpTransport | None = None,
    ) -> None:
        self.credentials = credentials
        self._transport = transport or requests.Session()

    def fetch_events(
        self,
        *,
        start: date,
        end: date,
        ext: str,
    ) -> list[IcuWorkoutEvent]:
        """Fetch and validate planned workouts for an inclusive date range."""
        response = self._request(
            self._transport.get,
            f"{_API_BASE}/athlete/{self.credentials.athlete_id}/events",
            params={
                "category": "WORKOUT",
                "oldest": start.isoformat(),
                "newest": end.isoformat(),
                "resolve": "true",
                "ext": ext,
            },
            auth=self._auth(),
            timeout=20,
        )
        try:
            return _ICU_WORKOUT_EVENTS.validate_python(response.json())
        except (ValidationError, ValueError) as exc:
            message = "returned unexpected data"
            raise IntegrationResponseError(
                _INTEGRATION_NAME,
                message,
            ) from exc

    def upload_tcx(self, name: str, data: bytes) -> IcuUploadResponse:
        """Upload TCX bytes and validate the returned activity identifier."""
        response = self._request(
            self._transport.post,
            f"{_API_BASE}/athlete/{self.credentials.athlete_id}/activities",
            auth=self._auth(),
            files={"file": (f"{name}.tcx", data, "application/vnd.garmin.tcx+xml")},
            timeout=60,
        )
        return IcuUploadResponse.from_response(response)

    def _auth(self) -> HTTPBasicAuth:
        """Build the shared Intervals.icu Basic authentication header."""
        return HTTPBasicAuth("API_KEY", self.credentials.api_key)

    @staticmethod
    def _request(
        request: Callable[..., requests.Response],
        url: str,
        **kwargs: object,
    ) -> requests.Response:
        try:
            response = request(url, **kwargs)
            response.raise_for_status()
        except requests.RequestException as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            message = str(exc)
            raise IntegrationTransportError(
                _INTEGRATION_NAME,
                message,
                status_code=status_code,
            ) from exc
        return response

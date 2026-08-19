"""Typed Intervals.icu credentials, transport, and response models."""

from __future__ import annotations

import gzip
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
        """Parse current and legacy upload responses and require an activity ID."""
        try:
            raw_payload = response.json()
            if isinstance(raw_payload, list):
                payloads = _ICU_UPLOAD_RESPONSES.validate_python(raw_payload)
            elif isinstance(raw_payload, dict) and "activities" in raw_payload:
                envelope = _ICU_UPLOAD_ENVELOPE.validate_python(raw_payload)
                payloads = envelope.activities
            else:
                payloads = [cls.model_validate(raw_payload)]
        except (ValidationError, ValueError) as exc:
            message = "returned unexpected data"
            raise IntegrationResponseError(
                _INTEGRATION_NAME,
                message,
                debug_detail=_response_debug_detail(response),
            ) from exc

        if not payloads:
            message = "returned no upload result"
            raise IntegrationResponseError(
                _INTEGRATION_NAME,
                message,
                debug_detail=_response_debug_detail(response),
            )
        payload = payloads[0]

        if payload.provider_id is None:
            message = "returned no activity identifier"
            raise IntegrationResponseError(
                _INTEGRATION_NAME,
                message,
                debug_detail=_response_debug_detail(response),
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
_ICU_UPLOAD_RESPONSES = TypeAdapter(list[IcuUploadResponse])


class _IcuUploadEnvelope(BaseModel):
    """Current Intervals.icu upload wrapper containing created activities."""

    model_config = ConfigDict(extra="ignore")

    activities: list[IcuUploadResponse]


_ICU_UPLOAD_ENVELOPE = TypeAdapter(_IcuUploadEnvelope)


def _response_debug_detail(
    response: requests.Response,
    fallback: str | None = None,
) -> str | None:
    """Return a compact provider response without exposing the request URL."""
    response_text = getattr(response, "text", None)
    if isinstance(response_text, str) and response_text.strip():
        return " ".join(response_text.split())[:500]
    reason = getattr(response, "reason", None)
    if reason:
        return str(reason)[:500]
    return fallback


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
        """Gzip TCX bytes, upload them, and validate the activity identifier."""
        compressed = gzip.compress(data, compresslevel=6, mtime=0)
        response = self._request(
            self._transport.post,
            f"{_API_BASE}/athlete/{self.credentials.athlete_id}/activities",
            auth=self._auth(),
            files={"file": (f"{name}.tcx.gz", compressed, "application/gzip")},
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
            error_response = exc.response
            status_code = error_response.status_code if error_response is not None else None
            debug_detail = type(exc).__name__
            if error_response is not None:
                debug_detail = _response_debug_detail(error_response, debug_detail)
            message = "request failed"
            raise IntegrationTransportError(
                _INTEGRATION_NAME,
                message,
                status_code=status_code,
                debug_detail=debug_detail,
            ) from exc
        return response

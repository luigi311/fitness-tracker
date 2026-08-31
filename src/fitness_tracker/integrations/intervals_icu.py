"""Typed Intervals.icu credentials, transport, and response models."""

from __future__ import annotations

import gzip
import re
from collections import Counter
from datetime import date, datetime
from time import monotonic
from typing import TYPE_CHECKING, Protocol

import requests
from loguru import logger
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
_URL_PATTERN = re.compile(r"\b(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)
_ATHLETE_PATH_PATTERN = re.compile(
    r"(/athlete/)[^/\s?&#<>\"']+",
    re.IGNORECASE,
)


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


def _payload_shape(payload: object) -> str:
    """Describe a JSON payload without logging provider-supplied values."""
    if isinstance(payload, list):
        return f"list(length={len(payload)})"
    if isinstance(payload, dict):
        keys = sorted(str(key) for key in payload)[:20]
        suffix = ", ..." if len(payload) > len(keys) else ""
        return f"object(keys=[{', '.join(keys)}{suffix}])"
    return type(payload).__name__


def _validation_debug_detail(payload: object, error: ValidationError) -> str:
    """Summarize schema errors without including response field values."""
    problems = []
    for problem in error.errors(include_input=False, include_url=False)[:10]:
        location = ".".join(str(part) for part in problem.get("loc", ())) or "response"
        problems.append(
            f"{location}: {problem.get('type', 'validation_error')} "
            f"({problem.get('msg', 'invalid value')})",
        )
    suffix = f"; errors={'; '.join(problems)}" if problems else ""
    return f"payload={_payload_shape(payload)}{suffix}"[:500]


def _redact_values(detail: str | None, values: tuple[str, ...]) -> str | None:
    """Remove caller-known credentials and identifiers from provider detail."""
    if detail is None:
        return None
    for value in values:
        if value:
            detail = detail.replace(value, "[redacted]")
    return detail


def _local_date_iso(value: date) -> str:
    """Format a date-like value as the local calendar date required by the API."""
    if isinstance(value, datetime):
        value = value.date()
    return value.isoformat()


def _response_debug_detail(
    response: requests.Response,
    fallback: str | None = None,
) -> str | None:
    """Return a compact provider response without exposing the request URL."""
    response_text = getattr(response, "text", None)
    if isinstance(response_text, str) and response_text.strip():
        detail = " ".join(response_text.split())
    else:
        reason = getattr(response, "reason", None)
        detail = str(reason) if reason else fallback
    if detail is None:
        return None

    detail = detail.replace("\\/", "/")
    request_url = getattr(response, "url", None)
    if isinstance(request_url, str) and request_url:
        detail = detail.replace(request_url, "[redacted URL]")
    detail = _URL_PATTERN.sub("[redacted URL]", detail)
    return _ATHLETE_PATH_PATTERN.sub(r"\1[redacted]", detail)[:500]


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
        oldest = _local_date_iso(start)
        newest = _local_date_iso(end)
        logger.debug(
            "Intervals.icu event fetch starting: oldest={}, newest={}, ext={}",
            oldest,
            newest,
            ext,
        )
        response = self._request(
            self._transport.get,
            f"{_API_BASE}/athlete/{self.credentials.athlete_id}/events",
            operation="fetch events",
            redactions=(self.credentials.athlete_id, self.credentials.api_key),
            params={
                "category": "WORKOUT",
                "oldest": oldest,
                "newest": newest,
                "resolve": "true",
                "ext": ext,
            },
            auth=self._auth(),
            timeout=20,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            detail = _response_debug_detail(response, type(exc).__name__)
            detail = _redact_values(
                detail,
                (self.credentials.athlete_id, self.credentials.api_key),
            )
            logger.warning(
                "Intervals.icu event response was not valid JSON: status={}, detail={}",
                getattr(response, "status_code", None),
                detail,
            )
            message = "returned unexpected data"
            raise IntegrationResponseError(
                _INTEGRATION_NAME,
                message,
                debug_detail=detail,
            ) from exc
        logger.trace("Intervals.icu event response JSON shape: {}", _payload_shape(payload))
        try:
            events = _ICU_WORKOUT_EVENTS.validate_python(payload)
        except ValidationError as exc:
            detail = _validation_debug_detail(payload, exc)
            logger.warning("Intervals.icu event response validation failed: {}", detail)
            message = "returned unexpected data"
            raise IntegrationResponseError(
                _INTEGRATION_NAME,
                message,
                debug_detail=detail,
            ) from exc

        event_types = Counter((event.type.strip() or "<empty>") for event in events)
        logger.debug(
            "Intervals.icu event fetch completed: events={}, event_types={}",
            len(events),
            dict(sorted(event_types.items())),
        )
        for index, event in enumerate(events):
            logger.trace(
                "Intervals.icu event summary: index={}, type={!r}, planned_date={}, "
                "has_filename={}, has_workout_data={}",
                index,
                event.type,
                event.planned_date,
                bool(event.workout_filename),
                bool(event.workout_file_base64),
            )
        return events

    def upload_tcx(self, name: str, data: bytes) -> IcuUploadResponse:
        """Gzip TCX bytes, upload them, and validate the activity identifier."""
        compressed = gzip.compress(data, compresslevel=6, mtime=0)
        response = self._request(
            self._transport.post,
            f"{_API_BASE}/athlete/{self.credentials.athlete_id}/activities",
            operation="upload activity",
            redactions=(self.credentials.athlete_id, self.credentials.api_key),
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
        *,
        operation: str = "request",
        redactions: tuple[str, ...] = (),
        **kwargs: object,
    ) -> requests.Response:
        method = getattr(request, "__name__", type(request).__name__).upper()
        params = kwargs.get("params")
        param_keys = sorted(str(key) for key in params) if isinstance(params, dict) else []
        timeout = kwargs.get("timeout")
        logger.trace(
            "Intervals.icu request starting: operation={}, method={}, timeout_s={}, "
            "query_keys={}",
            operation,
            method,
            timeout,
            param_keys,
        )
        started = monotonic()
        try:
            response = request(url, **kwargs)
            response.raise_for_status()
        except requests.RequestException as exc:
            elapsed_ms = round((monotonic() - started) * 1000)
            error_response = exc.response
            status_code = error_response.status_code if error_response is not None else None
            debug_detail = type(exc).__name__
            if error_response is not None:
                debug_detail = _response_debug_detail(error_response, debug_detail)
            debug_detail = _redact_values(debug_detail, redactions)
            logger.warning(
                "Intervals.icu request failed: operation={}, method={}, status={}, "
                "elapsed_ms={}, error_type={}, detail={}",
                operation,
                method,
                status_code,
                elapsed_ms,
                type(exc).__name__,
                debug_detail,
            )
            message = "request failed"
            raise IntegrationTransportError(
                _INTEGRATION_NAME,
                message,
                status_code=status_code,
                debug_detail=debug_detail,
            ) from exc
        elapsed_ms = round((monotonic() - started) * 1000)
        headers = getattr(response, "headers", {})
        logger.trace(
            "Intervals.icu request completed: operation={}, method={}, status={}, "
            "elapsed_ms={}, content_type={}, content_length={}",
            operation,
            method,
            getattr(response, "status_code", None),
            elapsed_ms,
            headers.get("content-type") if hasattr(headers, "get") else None,
            headers.get("content-length") if hasattr(headers, "get") else None,
        )
        return response

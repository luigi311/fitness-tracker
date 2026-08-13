"""Lifecycle-safe background jobs for UI-facing services."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Lock, Thread
from typing import TypeVar, cast

from loguru import logger

from fitness_tracker.core.errors import UserActionableError

T = TypeVar("T")
UiCallback = Callable[[], bool | None]
MarshalToUi = Callable[[UiCallback], object]


class JobError(RuntimeError):
    """Base class for runner lifecycle errors."""


class DuplicateJobError(JobError):
    """A job with the requested name is already running or delivering."""


class JobRunnerShutdownError(JobError):
    """A new job was submitted after the runner shut down."""


class JobCancelledError(JobError):
    """A cancellable worker noticed that its cancellation token was set."""


class CancellationToken:
    """Cooperative cancellation state passed to a background worker."""

    def __init__(self) -> None:
        self._event = Event()

    @property
    def cancelled(self) -> bool:
        """Whether cancellation has been requested."""
        return self._event.is_set()

    def cancel(self) -> None:
        """Request cancellation; workers decide when to stop."""
        self._event.set()

    def raise_if_cancelled(self) -> None:
        """Raise :class:`JobCancelled` when the worker should stop."""
        if self.cancelled:
            raise JobCancelledError

    def wait(self, timeout: float) -> bool:
        """Wait for cancellation, returning whether cancellation was requested."""
        return self._event.wait(timeout)


@dataclass(frozen=True)
class JobHandle:
    """Handle used to cancel one submitted job."""

    name: str
    token: CancellationToken

    def cancel(self) -> None:
        """Request cooperative cancellation for this job."""
        self.token.cancel()


@dataclass
class _Job:
    name: str
    work: Callable[[CancellationToken], object]
    on_success: Callable[[object], None] | None
    on_error: Callable[[Exception], None] | None
    on_finally: Callable[[], None] | None
    on_discard: Callable[[], None] | None
    token: CancellationToken
    delivered: bool = False


class BackgroundJobRunner:
    """Run named background operations and marshal their terminal callbacks."""

    _MARSHAL_MAX_ATTEMPTS = 5
    _MARSHAL_RETRY_DELAY_S = 0.1

    def __init__(self, marshal_to_ui: MarshalToUi) -> None:
        self._marshal_to_ui = marshal_to_ui
        self._lock = Lock()
        self._jobs: dict[str, _Job] = {}
        self._shutdown = False

    def submit(
        self,
        name: str,
        work: Callable[[CancellationToken], T],
        *,
        on_success: Callable[[T], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        on_finally: Callable[[], None] | None = None,
        on_discard: Callable[[], None] | None = None,
    ) -> JobHandle:
        """Start a named job, rejecting duplicate names until delivery completes."""
        with self._lock:
            if self._shutdown:
                raise JobRunnerShutdownError
            if name in self._jobs:
                raise DuplicateJobError(name)
            token = CancellationToken()
            job = _Job(
                name=name,
                work=cast("Callable[[CancellationToken], object]", work),
                on_success=cast("Callable[[object], None] | None", on_success),
                on_error=on_error,
                on_finally=on_finally,
                on_discard=on_discard,
                token=token,
            )
            self._jobs[name] = job

        thread = Thread(target=self._run, args=(job,), name=f"job:{name}", daemon=True)
        thread.start()
        return JobHandle(name=name, token=token)

    def cancel(self, name: str) -> bool:
        """Request cancellation for a named job, returning whether it existed."""
        with self._lock:
            job = self._jobs.get(name)
        if job is None:
            return False
        job.token.cancel()
        return True

    def shutdown(self) -> None:
        """Cancel active jobs and prevent all future UI callback delivery."""
        with self._lock:
            self._shutdown = True
            jobs = tuple(self._jobs.values())
            self._jobs.clear()
        for job in jobs:
            job.token.cancel()

    def _run(self, job: _Job) -> None:
        result: object = None
        error: Exception | None = None
        try:
            result = job.work(job.token)
        except Exception as exc:
            error = self._classify_and_log(job.name, exc)

        def deliver() -> bool:
            self._deliver(job, result, error)
            return False

        marshal_failures = 0
        while True:
            try:
                self._marshal_to_ui(deliver)
            except Exception:
                if job.delivered:
                    return
                marshal_failures += 1
                if marshal_failures == 1:
                    logger.exception(
                        "Could not marshal completion for background job {}; retrying",
                        job.name,
                    )
                if marshal_failures >= self._MARSHAL_MAX_ATTEMPTS:
                    self._discard_delivery(job)
                    logger.error(
                        "Could not marshal completion for background job {} after {} attempts; "
                        "dropping delivery",
                        job.name,
                        marshal_failures,
                    )
                    return
                if job.token.wait(self._MARSHAL_RETRY_DELAY_S):
                    self._discard_delivery(job)
                    return
            else:
                return

    def _deliver(
        self,
        job: _Job,
        result: object,
        error: Exception | None,
    ) -> None:
        with self._lock:
            if job.delivered:
                return
            job.delivered = True
            self._jobs.pop(job.name, None)
            if self._shutdown:
                return

        try:
            if not job.token.cancelled:
                if error is None:
                    success_callback = job.on_success
                    if success_callback is not None:
                        self._invoke_callback(
                            job.name,
                            "success",
                            lambda: success_callback(result),
                        )
                else:
                    error_callback = job.on_error
                    if error_callback is not None:
                        self._invoke_callback(
                            job.name,
                            "error",
                            lambda: error_callback(error),
                        )
        finally:
            if job.on_finally is not None:
                self._invoke_callback(job.name, "finally", job.on_finally)

    def _discard_delivery(self, job: _Job) -> None:
        """Drop a delivery and run its worker-safe discard hook, if any.

        ``on_discard`` is deliberately separate from ``on_finally``: the former
        runs on the worker when UI marshalling is unavailable and must not touch
        UI objects; the latter runs on the UI thread after a delivered callback.
        """
        with self._lock:
            if job.delivered:
                return
            job.delivered = True
            self._jobs.pop(job.name, None)
        if job.on_discard is not None:
            try:
                job.on_discard()
            except Exception:
                logger.exception("Background job {} discard callback failed", job.name)

    @staticmethod
    def _invoke_callback(name: str, kind: str, callback: Callable[[], None]) -> None:
        try:
            callback()
        except Exception:
            logger.exception("Background job {} {} callback failed", name, kind)

    @staticmethod
    def _classify_and_log(name: str, error: Exception) -> Exception:
        if isinstance(error, JobCancelledError):
            logger.debug("Background job {} cancelled", name)
            return error
        if isinstance(error, UserActionableError):
            logger.warning("Background job {} failed: {}", name, error)
            return error
        logger.opt(exception=error).error("Unexpected background job {} failure", name)
        return error

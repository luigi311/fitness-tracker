from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING

from loguru import logger

from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.exporters import activity_to_tcx, infer_sport
from fitness_tracker.integrations.errors import IntegrationError, IntegrationTransportError

if TYPE_CHECKING:
    from fitness_tracker.data.models import ActivityUpload
    from fitness_tracker.data.repositories import ActivityRepository
    from fitness_tracker.integrations.intervals_icu import IntervalsICUClient

PROVIDER_NAME = "intervals_icu"
UploadResult = tuple[int, bool, str | None]


@dataclass
class IntervalsICUUploader:
    """Upload finalized activities to Intervals.icu."""

    client: IntervalsICUClient

    @staticmethod
    def _reconcile_accepted_upload(
        repository: ActivityRepository,
        activity_id: int,
        upload: ActivityUpload,
    ) -> UploadResult:
        """Promote a durable remote acceptance without another provider request."""
        if not upload.provider_activity_id or not upload.payload_hash:
            error_text = "accepted upload is missing provider reconciliation metadata"
            logger.error(
                "Could not reconcile accepted Intervals.icu upload: activity_id={}, reason={}",
                activity_id,
                error_text,
            )
            return activity_id, False, error_text
        try:
            repository.mark_upload_ok(
                activity_id=activity_id,
                provider=PROVIDER_NAME,
                provider_activity_id=upload.provider_activity_id,
                payload_hash=upload.payload_hash,
            )
        except Exception as error:
            logger.exception(
                "Could not reconcile accepted Intervals.icu upload for activity_id={}",
                activity_id,
            )
            return activity_id, False, str(error)
        logger.info(
            "Reconciled accepted Intervals.icu upload: activity_id={}, provider_activity_id={}",
            activity_id,
            upload.provider_activity_id,
        )
        return activity_id, True, None

    @staticmethod
    def _store_remote_acceptance(
        repository: ActivityRepository,
        activity_id: int,
        provider_id: str,
        payload_hash: str,
    ) -> UploadResult:
        """Persist provider acceptance, retaining a recoverable state on failure."""
        logger.info(
            "Intervals.icu accepted upload: activity_id={}, provider_activity_id={}",
            activity_id,
            provider_id,
        )
        try:
            repository.mark_upload_ok(
                activity_id=activity_id,
                provider=PROVIDER_NAME,
                provider_activity_id=provider_id,
                payload_hash=payload_hash,
            )
        except Exception:
            logger.exception(
                "Intervals.icu accepted activity_id={}, but saving the local success state "
                "failed; retrying local update",
                activity_id,
            )
            try:
                repository.mark_upload_ok(
                    activity_id=activity_id,
                    provider=PROVIDER_NAME,
                    provider_activity_id=provider_id,
                    payload_hash=payload_hash,
                )
            except Exception as retry_error:
                logger.exception(
                    "Intervals.icu accepted activity_id={}, but the local success state "
                    "could not be saved after retry",
                    activity_id,
                )
                try:
                    repository.mark_upload_accepted(
                        activity_id=activity_id,
                        provider=PROVIDER_NAME,
                        provider_activity_id=provider_id,
                        payload_hash=payload_hash,
                        error_message=str(retry_error),
                    )
                except Exception as recovery_error:
                    logger.exception(
                        "Could not store recoverable accepted-upload state for activity_id={}",
                        activity_id,
                    )
                    return activity_id, False, str(recovery_error)
                logger.warning(
                    "Stored recoverable accepted-upload state: activity_id={}, "
                    "provider_activity_id={}",
                    activity_id,
                    provider_id,
                )
                return activity_id, True, None

        logger.info(
            "Stored successful upload state: activity_id={}, provider={}",
            activity_id,
            PROVIDER_NAME,
        )
        return activity_id, True, None

    def upload_not_uploaded(
        self,
        repository: ActivityRepository,
    ) -> list[UploadResult]:
        """Upload all activities that don't have an OK upload row for Intervals.icu."""
        out: list[UploadResult] = []
        acts = repository.list_not_uploaded(PROVIDER_NAME)
        if not acts:
            return out

        for a in acts:
            existing_upload = repository.get_activity_upload(a.id, PROVIDER_NAME)
            if existing_upload is not None and existing_upload.status == "accepted":
                out.append(self._reconcile_accepted_upload(repository, a.id, existing_upload))
                continue

            hrs = repository.list_heart_rates(a.id)
            runs = repository.list_running_metrics(a.id)
            cycles = repository.list_cycling_metrics(a.id)
            locations = repository.list_location_points(a.id)
            sport_row = repository.get_activity_sport(a.id)
            sport_type = (
                SportTypesEnum(sport_row.sport_type_id)
                if sport_row
                else infer_sport(hrs, runs, cycles, a.id)
            )
            if sport_type == SportTypesEnum.unknown:
                logger.warning(
                    "Intervals.icu upload skipped: activity_id={}, reason=unknown sport",
                    a.id,
                )
                continue
            prefix = "Run" if sport_type == SportTypesEnum.running else "Ride"
            name = a.start_time.astimezone().strftime(f"{prefix}_%Y-%m-%d_%H-%M")
            logger.info(
                "Intervals.icu upload starting: activity_id={}, name={}, sport={}, "
                "samples(hr={}, running={}, cycling={}, location={})",
                a.id,
                name,
                sport_type.name,
                len(hrs),
                len(runs),
                len(cycles),
                len(locations),
            )
            stage = "TCX export"
            try:
                tcx = activity_to_tcx(
                    act=a,
                    heart_rates=hrs,
                    running=runs,
                    cycling=cycles,
                    locations=locations,
                    sport_type=sport_type,
                )
                # Simple content hash (helps our own dedupe/debug)
                phash = sha256(tcx).hexdigest()
                stage = "provider request"
                provider_id = self.client.upload_tcx(name, tcx).provider_id or phash
            except Exception as error:
                error_text = str(error)
                status_code = (
                    error.status_code if isinstance(error, IntegrationTransportError) else None
                )
                debug_detail = error.debug_detail if isinstance(error, IntegrationError) else None
                logger.error(
                    "Intervals.icu upload failed: activity_id={}, stage={}, error_type={}, "
                    "http_status={}, reason={}, response_detail={}",
                    a.id,
                    stage,
                    type(error).__name__,
                    status_code,
                    error_text,
                    debug_detail,
                )
                if not isinstance(error, IntegrationError):
                    logger.exception(
                        "Unexpected Intervals.icu upload failure for activity_id={}",
                        a.id,
                    )
                repository.mark_upload_failed(a.id, PROVIDER_NAME, error_text)
                logger.info(
                    "Stored failed upload state: activity_id={}, provider={}, reason={}",
                    a.id,
                    PROVIDER_NAME,
                    error_text,
                )
                out.append((a.id, False, error_text))
                continue

            out.append(self._store_remote_acceptance(repository, a.id, provider_id, phash))
        return out

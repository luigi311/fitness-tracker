from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING

from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.exporters import activity_to_tcx, infer_sport
from fitness_tracker.integrations.errors import IntegrationError

if TYPE_CHECKING:
    from fitness_tracker.data.repositories import ActivityRepository
    from fitness_tracker.integrations.intervals_icu import IntervalsICUClient

PROVIDER_NAME = "intervals_icu"


@dataclass
class IntervalsICUUploader:
    """Upload finalized activities to Intervals.icu."""

    client: IntervalsICUClient

    def upload_not_uploaded(
        self,
        repository: ActivityRepository,
    ) -> list[tuple[int, bool, str | None]]:
        """Upload all activities that don't have an OK upload row for Intervals.icu."""
        out: list[tuple[int, bool, str | None]] = []
        acts = repository.list_not_uploaded(PROVIDER_NAME)
        if not acts:
            return out

        for a in acts:
            hrs = repository.list_heart_rates(a.id)
            runs = repository.list_running_metrics(a.id)
            cycles = repository.list_cycling_metrics(a.id)
            sport_row = repository.get_activity_sport(a.id)
            sport_type = (
                SportTypesEnum(sport_row.sport_type_id)
                if sport_row
                else infer_sport(hrs, runs, cycles, a.id)
            )
            if sport_type == SportTypesEnum.unknown:
                continue
            try:
                tcx = activity_to_tcx(
                    act=a,
                    heart_rates=hrs,
                    running=runs,
                    cycling=cycles,
                    sport_type=sport_type,
                )
                # Simple content hash (helps our own dedupe/debug)
                phash = sha256(tcx).hexdigest()
                prefix = "Run" if sport_type == SportTypesEnum.running else "Ride"
                name = a.start_time.astimezone().strftime(f"{prefix}_%Y-%m-%d_%H-%M")
                provider_id = self.client.upload_tcx(name, tcx).provider_id

                repository.mark_upload_ok(
                    activity_id=a.id,
                    provider=PROVIDER_NAME,
                    provider_activity_id=provider_id,
                    payload_hash=phash,
                )
                out.append((a.id, True, None))
            except IntegrationError as e:
                repository.mark_upload_failed(a.id, PROVIDER_NAME, str(e))
                out.append((a.id, False, str(e)))
            except Exception as e:
                repository.mark_upload_failed(a.id, PROVIDER_NAME, str(e))
                out.append((a.id, False, str(e)))
        return out

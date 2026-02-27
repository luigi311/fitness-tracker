from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING

import requests
from loguru import logger
from requests.auth import HTTPBasicAuth

from fitness_tracker.database import CyclingMetrics, HeartRate, RunningMetrics
from fitness_tracker.exporters import activity_to_tcx, infer_sport

if TYPE_CHECKING:
    from fitness_tracker.ui import FitnessAppUI

API_BASE = "https://intervals.icu/api/v1"
PROVIDER_NAME = "intervals_icu"


@dataclass
class IntervalsICUUploader:
    athlete_id: str
    api_key: str

    def _auth(self):
        # Intervals.icu supports Basic auth with username "API_KEY" and your key as password,
        # or "Bearer" token. We use Basic for maximum compatibility.
        # https://forum.intervals.icu/t/api-access-to-intervals-icu/609  :contentReference[oaicite:0]{index=0}
        return HTTPBasicAuth("API_KEY", self.api_key)

    def _upload_tcx_bytes(self, name: str, data: bytes) -> requests.Response:
        url = f"{API_BASE}/athlete/{self.athlete_id or '0'}/activities"
        files = {"file": (f"{name}.tcx", data, "application/vnd.garmin.tcx+xml")}
        # Intervals does dedupe on upload (content hash). If already uploaded, you might get 200/409.
        # (Behavior described across forum/API threads.)
        resp = requests.post(url, auth=self._auth(), files=files, timeout=60)
        resp.raise_for_status()
        return resp

    def upload_not_uploaded(self, app: FitnessAppUI) -> list[tuple[int, bool, str | None]]:
        """Upload all activities that don't have an OK upload row for Intervals.icu."""
        out: list[tuple[int, bool, str | None]] = []
        if not self.api_key:
            return [(0, False, "Missing Intervals.icu API key")]

        if not app.recorder:
            return [(0, False, "No database available")]

        db = app.recorder.db
        acts = db.list_not_uploaded(PROVIDER_NAME)
        if not acts:
            return out

        with db.Session() as session:
            for a in acts:
                hrs = (
                    session.query(HeartRate)
                    .filter_by(activity_id=a.id)
                    .order_by(HeartRate.timestamp_ms)
                    .all()
                )
                runs = (
                    session.query(RunningMetrics)
                    .filter_by(activity_id=a.id)
                    .order_by(RunningMetrics.timestamp_ms)
                    .all()
                )
                cycles = (
                    session.query(CyclingMetrics)
                    .filter_by(activity_id=a.id)
                    .order_by(CyclingMetrics.timestamp_ms)
                    .all()
                )
                try:
                    sport = infer_sport(runs, cycles, hrs, a.id)
                    if sport == "Unknown":
                        continue

                    tcx = activity_to_tcx(
                        act=a,
                        heart_rates=hrs,
                        running=runs,
                        cycling=cycles,
                        sport=sport,
                    )
                    # Simple content hash (helps our own dedupe/debug)
                    phash = sha256(tcx).hexdigest()
                    prefix = "Run" if sport == "Running" else "Ride"
                    name = a.start_time.astimezone().strftime(f"{prefix}_%Y-%m-%d_%H-%M")
                    resp = self._upload_tcx_bytes(name, tcx)

                    # If the API returns an id in JSON, store it
                    provider_id = None
                    try:
                        j = resp.json()
                        provider_id = str(j.get("id") or j.get("activityId") or "")
                    except Exception:
                        provider_id = None

                    db.mark_upload_ok(
                        activity_id=int(a.id),
                        provider=PROVIDER_NAME,
                        provider_activity_id=provider_id,
                        payload_hash=phash,
                    )
                    out.append((int(a.id), True, None))
                except requests.HTTPError as e:
                    msg = f"{e.response.status_code} {e.response.reason}"
                    db.mark_upload_failed(int(a.id), PROVIDER_NAME, msg)
                    out.append((int(a.id), False, msg))
                except Exception as e:
                    db.mark_upload_failed(int(a.id), PROVIDER_NAME, str(e))
                    out.append((int(a.id), False, str(e)))
        return out

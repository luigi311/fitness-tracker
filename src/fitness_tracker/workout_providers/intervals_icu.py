from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import requests

from .utils import DownloadedWorkout, Sport

if TYPE_CHECKING:
    from collections.abc import Iterable

_API_BASE = "https://intervals.icu/api/v1"

@dataclass
class IntervalsICUProvider:
    athlete_id: str
    api_key: str
    ext: Literal["fit", "zwo", "erg", "mrc"] = "fit"

    def _auth(self):
        return requests.auth.HTTPBasicAuth("API_KEY", self.api_key)

    def fetch_between(
        self,
        sport: Sport,
        start: date,
        end: date,
        out_dir: Path,
    ) -> Iterable[DownloadedWorkout]:
        """
        After a successful events request, remove all existing files in out_dir and
        replace them with the latest week's workouts for the chosen sport.
        """
        out_dir.mkdir(parents=True, exist_ok=True)

        params = {
            "category": "WORKOUT",
            "oldest": start.isoformat(),
            "newest": end.isoformat(),
            "resolve": "true",
            "ext": self.ext,  # include workout file payload
        }
        url = f"{_API_BASE}/athlete/{self.athlete_id}/events"

        # 1) Request & parse (raises on HTTP errors)
        r = requests.get(url, params=params, auth=self._auth(), timeout=20)
        r.raise_for_status()
        events = (
            r.json()
            if r.headers.get("content-type", "").startswith("application/json")
            else json.loads(r.text)
        )

        # 2) Build the new set of files entirely in-memory first
        new_payloads: list[tuple[date, str, bytes]] = []
        for ev in events:
            ev_type = (ev.get("type") or "").strip()
            if sport == "running" and ev_type != "Run":
                continue
            if sport == "cycling" and ev_type != "Ride":
                continue

            wf_b64 = ev.get("workout_file_base64")
            wf_name = ev.get("workout_filename")
            if not (wf_b64 and wf_name):
                continue

            start_date_str = ev.get("start_date_local") or ev.get("start_date")
            d = date.fromisoformat(start_date_str[:10]) if start_date_str else start

            title = (ev.get("name") or ev.get("title") or Path(wf_name).stem).strip()
            safe_title = "".join(c if c.isalnum() or c in " -_." else "_" for c in title).strip()

            try:
                payload = base64.b64decode(wf_b64)
            except Exception:
                continue  # skip malformed entries

            new_payloads.append((d, safe_title, payload))

        # 3) SUCCESSFUL request: clean the folder first (authoritative sync)
        #    Even if there are zero events, we still clear old files.
        def _is_workout_file(p: Path) -> bool:
            return p.is_file() and p.suffix.lower() in (".fit", ".zwo", ".erg", ".mrc")

        for old in list(out_dir.iterdir()):
            if _is_workout_file(old):
                try:
                    old.unlink()
                except Exception:
                    pass  # best-effort cleanup

        # 4) Write the new files
        written: list[DownloadedWorkout] = []
        for d, safe_title, payload in new_payloads:
            out_name = f"{d.isoformat()} {safe_title}.{self.ext}"
            out_path = out_dir / out_name
            try:
                out_path.write_bytes(payload)
                written.append(DownloadedWorkout(path=out_path, start_date=d, title=safe_title))
            except Exception:
                # continue writing others even if one fails
                continue

        return written

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .storage import Storage


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventLogClient:
    """Small queued HTTP client.

    Event Log API contract can be adjusted later without touching the backup engine.
    Expected endpoint accepts JSON POST and returns any 2xx response.
    """

    def __init__(self, storage: Storage):
        self.storage = storage

    def queue(self, job_id: str, payload: dict[str, Any]) -> None:
        self.storage.queue_event(job_id, payload, utc_now())

    def try_send_job(self, job_id: str) -> tuple[bool, str]:
        endpoint = (self.storage.get_setting("eventlog_url", "") or "").strip()
        enabled = bool(self.storage.get_setting("eventlog_enabled", True))
        if not enabled:
            self.storage.update_event_queue(job_id, "disabled", utc_now(), "Event Log sync disabled")
            return False, "Event Log sync disabled"
        if not endpoint:
            self.storage.update_event_queue(job_id, "not_configured", utc_now(), "Event Log URL not configured")
            return False, "Event Log URL not configured"

        queued = [q for q in self.storage.get_queued_events(200) if q["job_id"] == job_id]
        if not queued:
            return True, "No queued record"
        item = queued[0]
        payload = item["payload"].encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "TPS-Photo-Import/1.0"}
        api_key = (self.storage.get_setting("eventlog_api_key", "") or "").strip()
        if api_key:
            headers["X-TPS-Import-Key"] = api_key
        req = urllib.request.Request(
            endpoint,
            data=payload,
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as response:
                if 200 <= response.status < 300:
                    self.storage.update_event_queue(job_id, "sent", utc_now())
                    return True, f"Sent ({response.status})"
                msg = f"HTTP {response.status}"
        except Exception as exc:
            msg = str(exc)
        self.storage.update_event_queue(job_id, "queued", utc_now(), msg)
        return False, msg

    def sync_all(self) -> list[tuple[str, bool, str]]:
        results = []
        for item in self.storage.get_queued_events(100):
            ok, msg = self.try_send_job(item["job_id"])
            results.append((item["job_id"], ok, msg))
        return results
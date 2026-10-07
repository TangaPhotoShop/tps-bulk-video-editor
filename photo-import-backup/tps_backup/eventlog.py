from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .storage import Storage

DEFAULT_EVENTLOG_ENDPOINT = "https://tps-event-log-import-api-production.up.railway.app/api/photo-import"
DEFAULT_EVENTLOG_HEALTH = "https://tps-event-log-import-api-production.up.railway.app/health"\nDEFAULT_EVENTLOG_AUTH_TEST = "https://tps-event-log-import-api-production.up.railway.app/api/auth-test"
DEFAULT_EVENTLOG_IMPORT_KEY = "gkq-yDOoj8rao_HLsN4cPovcMBJXKb5aRpM-l8s5eSc"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventLogClient:
    """Queued Event Log HTTP client.

    Completed photo imports are always stored in the local SQLite queue first.
    Network/API failures never affect the photo backup itself; queued records retry later.
    """

    def __init__(self, storage: Storage):
        self.storage = storage

    def endpoint(self) -> str:
        configured = (self.storage.get_setting("eventlog_url", "") or "").strip()
        return configured or DEFAULT_EVENTLOG_ENDPOINT

    def api_key(self) -> str:
        configured = (self.storage.get_setting("eventlog_api_key", "") or "").strip()
        return configured or DEFAULT_EVENTLOG_IMPORT_KEY

    def queue(self, job_id: str, payload: dict[str, Any]) -> None:
        self.storage.queue_event(job_id, payload, utc_now())

    def test_connection(self) -> tuple[bool, str]:
        try:
            req = urllib.request.Request(
                DEFAULT_EVENTLOG_AUTH_TEST,
                method="GET",
                headers={
                    "User-Agent": "TPS-Photo-Import/1.2",
                    "X-TPS-Import-Key": self.api_key(),
                },
            )
            with urllib.request.urlopen(req, timeout=8) as response:
                raw = response.read().decode("utf-8", errors="replace")
                if 200 <= response.status < 300:
                    try:
                        data = json.loads(raw)
                        count = data.get("records")
                        suffix = f" • {count} imported record(s)" if isinstance(count, int) else ""
                    except Exception:
                        suffix = ""
                    return True, f"Connected and authorised{suffix}"
                return False, f"Event Log API returned HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return False, "Event Log API key was rejected"
            return False, f"Event Log API returned HTTP {exc.code}"
        except Exception as exc:
            return False, str(exc)

    def try_send_job(self, job_id: str) -> tuple[bool, str]:
        enabled = bool(self.storage.get_setting("eventlog_enabled", True))
        if not enabled:
            self.storage.update_event_queue(job_id, "disabled", utc_now(), "Event Log sync disabled")
            return False, "Event Log sync disabled"

        queued = [q for q in self.storage.get_queued_events(200) if q["job_id"] == job_id]
        if not queued:
            return True, "No queued record"

        item = queued[0]
        payload = item["payload"].encode("utf-8")
        req = urllib.request.Request(
            self.endpoint(),
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "TPS-Photo-Import/1.2",
                "X-TPS-Import-Key": self.api_key(),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                body = response.read().decode("utf-8", errors="replace")
                if 200 <= response.status < 300:
                    self.storage.update_event_queue(job_id, "sent", utc_now())
                    return True, f"Sent to Event Log ({response.status})"
                msg = f"HTTP {response.status}: {body[:300]}"
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            msg = f"HTTP {exc.code}: {body[:300] or exc.reason}"
        except Exception as exc:
            msg = str(exc)

        self.storage.update_event_queue(job_id, "queued", utc_now(), msg)
        return False, msg

    def sync_all(self) -> list[tuple[str, bool, str]]:
        results = []
        for item in self.storage.get_queued_events(200):
            ok, msg = self.try_send_job(item["job_id"])
            results.append((item["job_id"], ok, msg))
        return results

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .eventlog import DEFAULT_EVENTLOG_IMPORT_KEY
from .storage import Storage

REFERENCE_BASE = "https://tps-event-log-import-api-production.up.railway.app"


class ReferenceSyncError(RuntimeError):
    pass


class ReferenceSyncConflict(ReferenceSyncError):
    def __init__(self, message: str, current: dict[str, Any] | None = None):
        super().__init__(message)
        self.current = current or {}


class ReferenceDataClient:
    """Central photographer/event reference-data client with a local SQLite cache."""

    def __init__(self, storage: Storage):
        self.storage = storage

    def _headers(self) -> dict[str, str]:
        key = (self.storage.get_setting("eventlog_api_key", "") or "").strip() or DEFAULT_EVENTLOG_IMPORT_KEY
        return {
            "Content-Type": "application/json",
            "User-Agent": "TPS-Photo-Import/1.2",
            "X-TPS-Import-Key": key,
        }

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            REFERENCE_BASE + path,
            data=body,
            method=method,
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as response:
                raw = response.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw else {}
                if not data.get("ok", False):
                    raise ReferenceSyncError(data.get("error") or f"HTTP {response.status}")
                return data
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw else {}
            except Exception:
                data = {}
            if exc.code == 409 and data.get("conflict"):
                raise ReferenceSyncConflict(data.get("error") or "Reference data changed on another computer", data.get("current"))
            if exc.code == 401:
                raise ReferenceSyncError("Shared reference-data authorization was rejected")
            raise ReferenceSyncError(data.get("error") or f"HTTP {exc.code}") from exc
        except ReferenceSyncError:
            raise
        except Exception as exc:
            raise ReferenceSyncError(str(exc)) from exc

    def _apply(self, data: dict[str, Any]) -> dict[str, Any]:
        photographers = data.get("photographers")
        events = data.get("events")
        if not isinstance(photographers, list) or not isinstance(events, list):
            raise ReferenceSyncError("Shared reference data returned an invalid response")
        self.storage.set_setting("photographers", photographers)
        self.storage.set_setting("events", events)
        self.storage.set_setting("reference_revision", int(data.get("revision") or 0))
        self.storage.set_setting("reference_updated_at", data.get("updatedAt") or "")
        self.storage.set_setting("reference_last_sync_at", datetime.now(timezone.utc).isoformat())
        return data

    def get(self) -> dict[str, Any]:
        return self._request("GET", "/api/reference-data")

    def bootstrap(self) -> dict[str, Any]:
        payload = {
            "photographers": self.storage.get_setting("photographers", []),
            "events": self.storage.get_setting("events", []),
        }
        return self._apply(self._request("POST", "/api/reference-data/bootstrap", payload))

    def sync(self) -> dict[str, Any]:
        data = self.get()
        if not data.get("initialized"):
            return self.bootstrap()
        return self._apply(data)

    def push(self, photographers: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
        revision = int(self.storage.get_setting("reference_revision", 0) or 0)
        if revision < 1:
            self.sync()
            revision = int(self.storage.get_setting("reference_revision", 0) or 0)
        payload = {
            "expected_revision": revision,
            "photographers": photographers,
            "events": events,
        }
        return self._apply(self._request("PUT", "/api/reference-data", payload))

    def apply_server_copy(self, current: dict[str, Any]) -> dict[str, Any]:
        if not current:
            return self.sync()
        normalized = {
            "photographers": current.get("photographers", []),
            "events": current.get("events", []),
            "revision": current.get("revision", 0),
            "updatedAt": current.get("updatedAt", ""),
        }
        return self._apply(normalized)

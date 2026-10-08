from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

APP_NAME = "TPSPhotoImportBackup"


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if not base:
        base = str(Path.home() / ".tps_photo_import_backup")
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


DB_PATH = app_data_dir() / "tps_photo_backup.db"
MANIFEST_DIR = app_data_dir() / "manifests"
MANIFEST_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_SETTINGS: dict[str, Any] = {
    "backup1_path": "",
    "backup2_path": "",
    "emergency_path": str(Path.home() / "TPS Emergency Photo Backup"),
    "eventlog_url": "https://tps-event-log-import-api-production.up.railway.app/api/photo-import",
    "eventlog_api_key": "gkq-yDOoj8rao_HLsN4cPovcMBJXKb5aRpM-l8s5eSc",
    "eventlog_enabled": True,
    "dropbox_app_key": "",
    "dropbox_refresh_token": "",
    "dropbox_account_name": "",
    "dropbox_account_email": "",
    "dropbox_root_namespace_id": "",
    "dropbox_destination": "/TPS/TOUR PHOTO BACKUPS/From TEMP CLOUD FOLDER",
    "dropbox_enabled": True,
    "delete_after_verified_default": False,
    "photographers": [
        {"name": "Steve", "initials": "SW"},
        {"name": "Pete", "initials": "PD"},
        {"name": "Dylan", "initials": "DM"},
        {"name": "Hannah", "initials": "HT"},
        {"name": "Naoki", "initials": "NM"},
        {"name": "Nana", "initials": "NC"},
        {"name": "Summer", "initials": "SH"},
    ],
    "events": [
        {"name": "Desert Safari", "code": "SAND", "times": ["09:15", "11:00", "13:30", "15:15"], "guests_required": True},
        {"name": "ATV Quad Bikes", "code": "ATV", "times": ["09:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00"], "guests_required": True},
        {"name": "Dolphin Feed", "code": "DOL", "times": ["18:00"], "guests_required": False},
        {"name": "Sunset Photos", "code": "SUN", "times": ["17:00"], "guests_required": False},
        {"name": "Group Photos", "code": "GROUP", "times": [], "guests_required": False},
        {"name": "Other", "code": "OTHER", "times": [], "guests_required": False},
    ],
}


class Storage:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._ensure_defaults()

    @contextmanager
    def connect(self):
        """Open a SQLite connection and always close it after the operation."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    date TEXT NOT NULL,
                    photographer TEXT NOT NULL,
                    initials TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    event_code TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    guests INTEGER,
                    notes TEXT,
                    source_path TEXT NOT NULL,
                    selected_count INTEGER NOT NULL,
                    excluded_count INTEGER NOT NULL,
                    selected_bytes INTEGER NOT NULL,
                    folder_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    primary_status TEXT NOT NULL,
                    backup2_status TEXT NOT NULL,
                    backup3_status TEXT NOT NULL,
                    emergency_status TEXT NOT NULL,
                    eventlog_status TEXT NOT NULL,
                    sd_deleted INTEGER NOT NULL DEFAULT 0,
                    manifest_path TEXT NOT NULL,
                    error_message TEXT
                );
                CREATE TABLE IF NOT EXISTS event_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def _ensure_defaults(self) -> None:
        with self.connect() as conn:
            for key, value in DEFAULT_SETTINGS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                    (key, json.dumps(value)),
                )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def all_settings(self) -> dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        data = {}
        for row in rows:
            try:
                data[row["key"]] = json.loads(row["value"])
            except Exception:
                data[row["key"]] = row["value"]
        return data

    def upsert_job(self, job: dict[str, Any]) -> None:
        cols = [
            "id", "created_at", "updated_at", "date", "photographer", "initials",
            "event_name", "event_code", "event_time", "guests", "notes", "source_path",
            "selected_count", "excluded_count", "selected_bytes", "folder_name", "status",
            "primary_status", "backup2_status", "backup3_status", "emergency_status",
            "eventlog_status", "sd_deleted", "manifest_path", "error_message"
        ]
        values = [job.get(c) for c in cols]
        placeholders = ",".join("?" for _ in cols)
        assignments = ",".join(f"{c}=excluded.{c}" for c in cols[1:])
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO jobs({','.join(cols)}) VALUES({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {assignments}",
                values,
            )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list_jobs(self, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def queue_event(self, job_id: str, payload: dict[str, Any], updated_at: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO event_queue(job_id, payload, status, attempts, last_error, updated_at)
                VALUES(?, ?, 'queued', 0, NULL, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    payload=excluded.payload,
                    status='queued',
                    updated_at=excluded.updated_at
                """,
                (job_id, json.dumps(payload), updated_at),
            )

    def get_queued_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM event_queue WHERE status != 'sent' ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def update_event_queue(self, job_id: str, status: str, updated_at: str, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE event_queue
                SET status=?, attempts=attempts+1, last_error=?, updated_at=?
                WHERE job_id=?
                """,
                (status, error, updated_at, job_id),
            )
            conn.execute(
                "UPDATE jobs SET eventlog_status=?, updated_at=? WHERE id=?",
                (status, updated_at, job_id),
            )

    def save_manifest(self, job_id: str, manifest: dict[str, Any]) -> Path:
        path = MANIFEST_DIR / f"{job_id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        tmp.replace(path)
        return path

    def load_manifest(self, job_id: str) -> dict[str, Any] | None:
        path = MANIFEST_DIR / f"{job_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
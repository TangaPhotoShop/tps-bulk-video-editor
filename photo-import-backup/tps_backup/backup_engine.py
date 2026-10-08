from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Any

from .storage import Storage
from .eventlog import EventLogClient
from .dropbox_client import DROPBOX_DESTINATION
from .autocorrect import auto_correct_jpeg

CHUNK_SIZE = 8 * 1024 * 1024
JPEG_SUFFIXES = {".jpg", ".jpeg"}


class BackupError(RuntimeError):
    pass


@dataclass
class SourcePhoto:
    path: str
    original_name: str
    number: str
    selected: bool
    size: int
    renamed: str = ""
    sha256: str = ""


@dataclass
class BackupJobSpec:
    date_text: str
    photographer: str
    initials: str
    event_name: str
    event_code: str
    event_time: str
    guests: int | None
    issue_type: str
    notes: str
    source_path: str
    photos: list[SourcePhoto]
    delete_after_verified: bool = False


@dataclass
class ProgressEvent:
    stage: str
    message: str
    current: int = 0
    total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    rate_bps: float = 0.0


def safe_time_code(event_time: str, event_code: str = "") -> str:
    raw = (event_time or "").strip()
    if not raw:
        return "NA"
    try:
        hh_s, mm = raw.split(":", 1)
        hh = int(hh_s)
        hour12 = hh % 12 or 12
        code = (event_code or "").upper()
        # TPS existing Desert folder convention: 09:15 -> 9, 13:30 -> 130, 15:15 -> 315.
        if code == "SAND":
            known = {"09:15": "9", "11:00": "11", "13:30": "130", "15:15": "315"}
            if raw in known:
                return known[raw]
        if mm == "00":
            return str(hour12)
        return f"{hour12}{mm}"
    except Exception:
        return re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").upper() or "NA"


def date_code(date_text: str) -> str:
    # UI passes ISO yyyy-mm-dd. Also tolerate display-form dates.
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y"):
        try:
            dt = datetime.strptime(date_text, fmt)
            return dt.strftime("%d-%b-%Y").upper()
        except ValueError:
            pass
    raise BackupError(f"Invalid date: {date_text}")


def extract_camera_number(filename: str) -> str:
    stem = Path(filename).stem
    groups = re.findall(r"\d+", stem)
    if not groups:
        raise BackupError(f"No numeric file number found in {filename}")
    return groups[-1]


def discover_photos(source: str | Path) -> list[SourcePhoto]:
    root = Path(source)
    if not root.exists():
        return []
    dcim = root / "DCIM"
    scan_root = dcim if dcim.exists() and dcim.is_dir() else root
    candidates: list[Path] = []
    for p in scan_root.rglob("*"):
        if p.is_file() and p.suffix.lower() in JPEG_SUFFIXES:
            candidates.append(p)
    candidates.sort(key=lambda p: (str(p.parent).lower(), p.name.lower()))
    photos = []
    for p in candidates:
        try:
            number = extract_camera_number(p.name)
        except BackupError:
            number = ""
        photos.append(SourcePhoto(str(p), p.name, number, True, p.stat().st_size))
    return photos


def sha256_file(path: Path, progress_cb: Callable[[int], None] | None = None) -> str:
    h = hashlib.sha256()
    with path.open("rb", buffering=0) as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
            if progress_cb:
                progress_cb(len(chunk))
    return h.hexdigest()


def path_network_identity(path: Path) -> str:
    s = str(path)
    if s.startswith("\\\\"):
        parts = s.lstrip("\\").split("\\")
        return "\\\\" + "\\".join(parts[:2]).lower() if len(parts) >= 2 else s.lower()
    drive = path.drive.upper()
    return drive or str(path.anchor).lower()


class BackupEngine:
    def __init__(self, storage: Storage):
        self.storage = storage
        self.eventlog = EventLogClient(storage)
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def reset_cancel(self) -> None:
        self.cancel_event.clear()

    def preflight_path(self, raw_path: str, required_bytes: int) -> tuple[bool, str, int]:
        if not raw_path.strip():
            return False, "Not configured", 0
        path = Path(raw_path)
        try:
            if not path.exists():
                return False, "Not reachable", 0
            if not path.is_dir():
                return False, "Not a folder", 0
            usage = shutil.disk_usage(path)
            if usage.free < required_bytes:
                return False, f"Insufficient free space ({usage.free:,} bytes free)", usage.free
            # Stronger than os.access for network shares: actually create, flush and remove a tiny file.
            test = path / f".tps_write_test_{uuid.uuid4().hex}.tmp"
            with test.open("wb") as f:
                f.write(b"TPS")
                f.flush()
                os.fsync(f.fileno())
            test.unlink()
            return True, "Ready", usage.free
        except Exception as exc:
            return False, f"Unavailable: {exc}", 0

    def backup1_mode(self) -> str:
        mode = str(self.storage.get_setting("backup1_mode", "original") or "original").strip().lower()
        return "auto_correct" if mode == "auto_correct" else "original"

    def staging_base(self) -> Path:
        raw = str(self.storage.get_setting("emergency_path", "") or "").strip()
        if not raw:
            raise BackupError("Emergency / temporary safety location is not configured")
        return Path(raw) / "_TPS_TEMP_ORIGINAL_STAGING"

    def preflight_all(self, required_bytes: int) -> dict[str, tuple[bool, str, int]]:
        """Preflight active destinations for the selected Backup 1 mode."""
        mode = self.backup1_mode()
        if mode == "auto_correct":
            staging = self.staging_base()
            try:
                staging.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                return {
                    "backup1": self.preflight_path(self.storage.get_setting("backup1_path", ""), required_bytes),
                    "backup2": self.preflight_path(self.storage.get_setting("backup2_path", ""), required_bytes),
                    "staging": (False, f"Temporary safety location unavailable: {exc}", 0),
                }
            raw = {
                "backup1": self.storage.get_setting("backup1_path", ""),
                "backup2": self.storage.get_setting("backup2_path", ""),
                "staging": str(staging),
            }
            results = {k: self.preflight_path(v, required_bytes) for k, v in raw.items()}

            b1_norm = os.path.normcase(os.path.abspath(str(raw["backup1"]))) if raw["backup1"] else ""
            b2_norm = os.path.normcase(os.path.abspath(str(raw["backup2"]))) if raw["backup2"] else ""
            if b1_norm and b2_norm and b1_norm == b2_norm:
                results["backup1"] = (
                    False,
                    "Auto Correct mode requires Backup 1 and Backup 2 to be different folders",
                    results["backup1"][2],
                )

            if raw["backup2"] and results["backup2"][0] and results["staging"][0]:
                if path_network_identity(Path(raw["backup2"])) == path_network_identity(Path(raw["staging"])):
                    results["staging"] = (
                        False,
                        "Temporary safety copy must be on a different drive/share from Backup 2",
                        results["staging"][2],
                    )
            return results

        raw = {
            "backup1": self.storage.get_setting("backup1_path", ""),
            "backup2": self.storage.get_setting("backup2_path", ""),
        }
        results = {k: self.preflight_path(v, required_bytes) for k, v in raw.items()}
        groups: dict[str, list[str]] = {}
        for key, value in raw.items():
            if value:
                groups.setdefault(path_network_identity(Path(value)), []).append(key)
        for _, keys in groups.items():
            if len(keys) < 2 or not all(results[k][0] for k in keys):
                continue
            free = min(results[k][2] for k in keys)
            needed = required_bytes * len(keys)
            if free < needed:
                for k in keys:
                    results[k] = (False, f"Shared destination needs {needed:,} bytes free for {len(keys)} copies", free)
        return results

    def _build_names(self, spec: BackupJobSpec) -> tuple[str, list[SourcePhoto]]:
        event_time_code = safe_time_code(spec.event_time, spec.event_code)
        folder = f"{date_code(spec.date_text)}-{spec.event_code.upper()}-{event_time_code}-{spec.initials.upper()}"
        used: dict[str, int] = {}
        photos: list[SourcePhoto] = []
        for src in spec.photos:
            p = SourcePhoto(**asdict(src))
            if not p.selected:
                photos.append(p)
                continue
            number = p.number or extract_camera_number(p.original_name)
            base = number
            used[base] = used.get(base, 0) + 1
            suffix = "" if used[base] == 1 else f"-{used[base]:02d}"
            p.renamed = f"{folder}-{base}{suffix}.JPG"
            photos.append(p)
        return folder, photos

    def _make_job_id(self, spec: BackupJobSpec) -> str:
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        return f"TPSIMP-{stamp}-{spec.initials.upper()}-{uuid.uuid4().hex[:6].upper()}"

    def _copy_source_to_primary(
        self,
        src: Path,
        dest: Path,
        expected_size: int,
        progress_bytes: Callable[[int], None] | None = None,
    ) -> str:
        if self.cancel_event.is_set():
            raise BackupError("Backup cancelled")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            if dest.stat().st_size != expected_size:
                raise BackupError(f"Destination file already exists with different size: {dest}")
            # Resume-safe: verify the existing destination against the current SD original.
            source_hash = sha256_file(src)
            if sha256_file(dest) == source_hash:
                return source_hash
            raise BackupError(f"Destination file already exists with different data: {dest}")
        partial = dest.with_name(dest.name + ".tpspartial")
        if partial.exists():
            partial.unlink()
        h = hashlib.sha256()
        written = 0
        try:
            with src.open("rb", buffering=0) as rf, partial.open("wb", buffering=0) as wf:
                while True:
                    if self.cancel_event.is_set():
                        raise BackupError("Backup cancelled")
                    chunk = rf.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    wf.write(chunk)
                    h.update(chunk)
                    written += len(chunk)
                    if progress_bytes:
                        progress_bytes(len(chunk))
                wf.flush()
                os.fsync(wf.fileno())
            if written != expected_size or partial.stat().st_size != expected_size:
                raise BackupError(f"Size mismatch while writing {dest.name}")
            source_hash = h.hexdigest()
            dest_hash = sha256_file(partial)
            if dest_hash != source_hash:
                raise BackupError(f"SHA-256 mismatch for {dest.name}")
            if dest.exists():
                # Never silently overwrite a different file.
                if dest.stat().st_size == expected_size and sha256_file(dest) == source_hash:
                    partial.unlink(missing_ok=True)
                    return source_hash
                raise BackupError(f"Destination file already exists with different data: {dest}")
            partial.replace(dest)
            return source_hash
        except Exception:
            # Keep partial for diagnosis but never expose as JPG.
            raise

    def _copy_verified_file(self, src: Path, dest: Path, expected_size: int, expected_hash: str) -> None:
        if self.cancel_event.is_set():
            raise BackupError("Backup cancelled")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            if dest.stat().st_size == expected_size and sha256_file(dest) == expected_hash:
                return
            raise BackupError(f"Existing file differs from source: {dest}")
        partial = dest.with_name(dest.name + ".tpspartial")
        if partial.exists():
            partial.unlink()
        try:
            shutil.copyfile(src, partial)
            if partial.stat().st_size != expected_size:
                raise BackupError(f"Exact byte-size check failed: {dest.name}")
            if sha256_file(partial) != expected_hash:
                raise BackupError(f"SHA-256 verification failed: {dest.name}")
            partial.replace(dest)
        except Exception:
            raise

    def _verify_manifest_files(self, folder: Path, files: list[dict[str, Any]]) -> None:
        for item in files:
            p = folder / item["renamed"]
            if not p.exists():
                raise BackupError(f"Missing file: {p}")
            if p.stat().st_size != item["size"]:
                raise BackupError(f"Byte-size mismatch: {p.name}")
            if sha256_file(p) != item["sha256"]:
                raise BackupError(f"SHA-256 mismatch: {p.name}")

    def _verify_folder_count(self, folder: Path, expected_count: int) -> None:
        actual = 0
        partials = []
        for p in folder.iterdir():
            if p.is_file() and p.suffix.lower() in JPEG_SUFFIXES:
                actual += 1
            if p.is_file() and p.name.lower().endswith(".tpspartial"):
                partials.append(p.name)
        if partials:
            raise BackupError(f"Incomplete partial files remain in {folder}: {len(partials)}")
        if actual != expected_count:
            raise BackupError(f"File-count mismatch in {folder}: expected {expected_count}, found {actual}")

    def benchmark_path(self, raw_path: str, size_mb: int = 32) -> tuple[bool, str, float, float]:
        """Return (ok, message, write_MBps, read_MBps) using a disposable binary file."""
        path = Path(raw_path)
        ok, msg, _ = self.preflight_path(raw_path, size_mb * 1024 * 1024)
        if not ok:
            return False, msg, 0.0, 0.0
        test = path / f".tps_speed_test_{uuid.uuid4().hex}.bin"
        total = size_mb * 1024 * 1024
        block = os.urandom(min(CHUNK_SIZE, total))
        try:
            start = time.monotonic()
            remaining = total
            with test.open("wb", buffering=0) as f:
                while remaining > 0:
                    chunk = block[: min(len(block), remaining)]
                    f.write(chunk)
                    remaining -= len(chunk)
                f.flush()
                os.fsync(f.fileno())
            write_s = max(0.001, time.monotonic() - start)
            start = time.monotonic()
            read_bytes = 0
            with test.open("rb", buffering=0) as f:
                while True:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    read_bytes += len(chunk)
            read_s = max(0.001, time.monotonic() - start)
            if read_bytes != total:
                return False, "Speed-test byte count mismatch", 0.0, 0.0
            mb = total / (1024 * 1024)
            return True, "OK", mb / write_s, mb / read_s
        except Exception as exc:
            return False, str(exc), 0.0, 0.0
        finally:
            try:
                test.unlink(missing_ok=True)
            except Exception:
                pass

    def _write_manifest_copy(self, folder: Path, manifest: dict[str, Any]) -> None:
        path = folder / "TPS-IMPORT-MANIFEST.json"
        tmp = folder / "TPS-IMPORT-MANIFEST.json.tmp"
        tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _job_row(self, manifest: dict[str, Any], manifest_path: Path, error: str | None = None) -> dict[str, Any]:
        m = manifest
        selected = m["selected_files"]
        return {
            "id": m["job_id"],
            "created_at": m["created_at"],
            "updated_at": datetime.now().isoformat(),
            "date": m["event"]["date"],
            "photographer": m["event"]["photographer"],
            "initials": m["event"]["initials"],
            "event_name": m["event"]["event_name"],
            "event_code": m["event"]["event_code"],
            "event_time": m["event"]["event_time"],
            "guests": m["event"]["guests"],
            "notes": m["event"]["notes"],
            "source_path": m["source_path"],
            "selected_count": len(selected),
            "excluded_count": m["excluded_count"],
            "selected_bytes": sum(x["size"] for x in selected),
            "folder_name": m["folder_name"],
            "status": m["status"],
            "primary_status": m["backups"]["backup1"]["status"],
            "backup2_status": m["backups"]["backup2"]["status"],
            "backup3_status": m["backups"].get("dropbox", m["backups"].get("backup3", {"status": "pending"}))["status"],
            "emergency_status": m["backups"]["emergency"]["status"],
            "eventlog_status": m.get("eventlog_status", "queued"),
            "sd_deleted": 1 if m.get("sd_deleted") else 0,
            "manifest_path": str(manifest_path),
            "error_message": error,
        }

    def _emit(self, cb: Callable[[ProgressEvent], None] | None, **kwargs: Any) -> None:
        if cb:
            cb(ProgressEvent(**kwargs))

    def run_normal(self, spec: BackupJobSpec, progress: Callable[[ProgressEvent], None] | None = None) -> dict[str, Any]:
        if self.backup1_mode() == "auto_correct":
            return self._run_normal_autocorrect(spec, progress)

        """Create two independently verified filesystem copies, then release the SD card.

        Dropbox is intentionally asynchronous and is uploaded later from verified Backup 1,
        so cloud speed or internet availability never holds the photographer or SD card.
        """
        self.reset_cancel()
        folder_name, photos = self._build_names(spec)
        selected = [p for p in photos if p.selected]
        if not selected:
            raise BackupError("No photos selected")
        total_bytes = sum(p.size for p in selected)
        checks = self.preflight_all(total_bytes)
        unavailable = [k for k, v in checks.items() if not v[0]]
        if unavailable:
            details = "; ".join(f"{k}: {checks[k][1]}" for k in unavailable)
            raise BackupError(f"Required backup location unavailable. {details}")

        b1 = Path(self.storage.get_setting("backup1_path")) / folder_name
        b2 = Path(self.storage.get_setting("backup2_path")) / folder_name
        for d in (b1, b2):
            d.mkdir(parents=True, exist_ok=True)

        job_id = self._make_job_id(spec)
        created = datetime.now().isoformat()
        dropbox_status = "pending" if (self.storage.get_setting("dropbox_app_key", "") and self.storage.get_setting("dropbox_refresh_token", "")) else "not_connected"
        manifest: dict[str, Any] = {
            "schema": 2,
            "job_id": job_id,
            "created_at": created,
            "source_path": spec.source_path,
            "folder_name": folder_name,
            "event": {
                "date": spec.date_text,
                "photographer": spec.photographer,
                "initials": spec.initials,
                "event_name": spec.event_name,
                "event_code": spec.event_code,
                "event_time": spec.event_time,
                "guests": spec.guests,
                "issue_type": spec.issue_type,
                "notes": spec.notes,
            },
            "excluded_count": len([p for p in photos if not p.selected]),
            "selected_files": [],
            "backups": {
                "backup1": {"path": str(b1), "status": "pending"},
                "backup2": {"path": str(b2), "status": "pending"},
                "dropbox": {"path": f"{DROPBOX_DESTINATION}/{folder_name}", "status": dropbox_status, "uploaded": []},
                "emergency": {"path": "", "status": "not_used"},
            },
            "status": "running",
            "eventlog_status": "queued",
            "sd_deleted": False,
        }
        manifest_path = self.storage.save_manifest(job_id, manifest)
        self.storage.upsert_job(self._job_row(manifest, manifest_path))

        bytes_done = 0
        start_time = time.monotonic()
        try:
            # PRIMARY FIRST: SD -> Backup 1, hashing source while writing then re-hashing destination.
            for idx, photo in enumerate(selected, start=1):
                src = Path(photo.path)
                dest = b1 / photo.renamed
                self._emit(progress, stage="primary", message=f"Primary backup: {photo.original_name}", current=idx-1, total=len(selected), bytes_done=bytes_done, bytes_total=total_bytes, rate_bps=(bytes_done / max(0.001, time.monotonic()-start_time)))

                def add_bytes(n: int) -> None:
                    nonlocal bytes_done
                    bytes_done += n
                    self._emit(progress, stage="primary", message=f"Primary backup: {photo.original_name}", current=idx-1, total=len(selected), bytes_done=bytes_done, bytes_total=total_bytes, rate_bps=(bytes_done / max(0.001, time.monotonic()-start_time)))

                digest = self._copy_source_to_primary(src, dest, photo.size, add_bytes)
                photo.sha256 = digest
                manifest["selected_files"].append({
                    "source": photo.path, "original_name": photo.original_name, "number": photo.number,
                    "renamed": photo.renamed, "size": photo.size, "sha256": digest,
                })
                self.storage.save_manifest(job_id, manifest)

            self._verify_folder_count(b1, len(selected))
            manifest["backups"]["backup1"]["status"] = "verified"
            self._write_manifest_copy(b1, manifest)
            self.storage.save_manifest(job_id, manifest)
            self.storage.upsert_job(self._job_row(manifest, manifest_path))
            self._emit(progress, stage="primary_ready", message="Primary backup verified and ready", current=len(selected), total=len(selected), bytes_done=total_bytes, bytes_total=total_bytes, rate_bps=0)

            # SECOND LOCAL COPY: verified Backup 1 -> Backup 2.
            for i, item in enumerate(manifest["selected_files"], start=1):
                if self.cancel_event.is_set():
                    raise BackupError("Backup cancelled")
                if not b2.exists():
                    raise BackupError("Connection lost to Backup 2")
                src = b1 / item["renamed"]
                dest = b2 / item["renamed"]
                self._copy_verified_file(src, dest, item["size"], item["sha256"])
                self._emit(progress, stage="backup2", message=f"Backup 2: {item['renamed']}", current=i, total=len(selected), bytes_done=i, bytes_total=len(selected), rate_bps=0)
            self._verify_folder_count(b2, len(selected))
            manifest["backups"]["backup2"]["status"] = "verified"
            self._write_manifest_copy(b2, manifest)

            # Final local invariant. Dropbox is not part of the SD-card deletion gate.
            for folder in (b1, b2):
                self._verify_folder_count(folder, len(selected))
            if not all(manifest["backups"][k]["status"] == "verified" for k in ("backup1", "backup2")):
                raise BackupError("Final 2/2 local verification state is incomplete")

            manifest["status"] = "verified_2_of_2_dropbox_pending"
            self.storage.save_manifest(job_id, manifest)
            for folder in (b1, b2):
                self._write_manifest_copy(folder, manifest)
            self.storage.upsert_job(self._job_row(manifest, manifest_path))

            payload = self._event_payload(manifest)
            self.eventlog.queue(job_id, payload)
            sent, _ = self.eventlog.try_send_job(job_id)
            manifest["eventlog_status"] = "sent" if sent else "queued"

            # Normally UI asks for a final confirmation after the two local copies are safe.
            if spec.delete_after_verified:
                self.delete_verified_sources(manifest)
                manifest["sd_deleted"] = True

            self.storage.save_manifest(job_id, manifest)
            for folder in (b1, b2):
                self._write_manifest_copy(folder, manifest)
            self.storage.upsert_job(self._job_row(manifest, manifest_path))
            self._emit(progress, stage="complete", message="2/2 local backups verified byte-for-byte — Dropbox can continue in background", current=len(selected), total=len(selected), bytes_done=total_bytes, bytes_total=total_bytes, rate_bps=0)
            return manifest

        except Exception as exc:
            manifest["status"] = "interrupted"
            manifest["sd_deleted"] = False
            manifest["error"] = str(exc)
            self.storage.save_manifest(job_id, manifest)
            self.storage.upsert_job(self._job_row(manifest, manifest_path, str(exc)))
            self._emit(progress, stage="error", message=str(exc), current=0, total=len(selected), bytes_done=bytes_done, bytes_total=total_bytes, rate_bps=0)
            raise

    def _run_normal_autocorrect(self, spec: BackupJobSpec, progress: Callable[[ProgressEvent], None] | None = None) -> dict[str, Any]:
        """Protect exact originals twice, then release the SD card.

        Backup 2 is the permanent untouched local archive.
        Temporary staging is a second exact original copy until Dropbox verifies.
        Backup 1 is built separately as a corrected working set.
        """
        self.reset_cancel()
        folder_name, photos = self._build_names(spec)
        selected = [p for p in photos if p.selected]
        if not selected:
            raise BackupError("No photos selected")
        total_bytes = sum(p.size for p in selected)
        checks = self.preflight_all(total_bytes)
        unavailable = [k for k, v in checks.items() if not v[0]]
        if unavailable:
            details = "; ".join(f"{k}: {checks[k][1]}" for k in unavailable)
            raise BackupError(f"Required location unavailable. {details}")

        b1 = Path(self.storage.get_setting("backup1_path")) / folder_name
        b2 = Path(self.storage.get_setting("backup2_path")) / folder_name
        staging = self.staging_base() / folder_name
        b2.mkdir(parents=True, exist_ok=True)
        staging.mkdir(parents=True, exist_ok=True)

        job_id = self._make_job_id(spec)
        created = datetime.now().isoformat()
        dropbox_status = "pending" if (self.storage.get_setting("dropbox_app_key", "") and self.storage.get_setting("dropbox_refresh_token", "")) else "not_connected"
        manifest: dict[str, Any] = {
            "schema": 3,
            "job_id": job_id,
            "created_at": created,
            "backup_mode": "auto_correct",
            "source_path": spec.source_path,
            "folder_name": folder_name,
            "event": {
                "date": spec.date_text,
                "photographer": spec.photographer,
                "initials": spec.initials,
                "event_name": spec.event_name,
                "event_code": spec.event_code,
                "event_time": spec.event_time,
                "guests": spec.guests,
                "issue_type": spec.issue_type,
                "notes": spec.notes,
            },
            "excluded_count": len([p for p in photos if not p.selected]),
            "selected_files": [],
            "autocorrect_files": [],
            "backups": {
                "backup1": {"path": str(b1), "status": "autocorrect_pending", "kind": "working_corrected"},
                "backup2": {"path": str(b2), "status": "pending", "kind": "original_archive"},
                "staging": {"path": str(staging), "status": "pending", "kind": "temporary_original_safety"},
                "dropbox": {"path": f"{DROPBOX_DESTINATION}/{folder_name}", "status": dropbox_status, "uploaded": [], "kind": "original_cloud_archive"},
                "emergency": {"path": "", "status": "not_used"},
            },
            "status": "running",
            "eventlog_status": "queued",
            "sd_deleted": False,
        }
        manifest_path = self.storage.save_manifest(job_id, manifest)
        self.storage.upsert_job(self._job_row(manifest, manifest_path))

        bytes_done = 0
        start_time = time.monotonic()
        try:
            # SD -> Backup 2 exact originals.
            for idx, photo in enumerate(selected, start=1):
                src = Path(photo.path)
                dest = b2 / photo.renamed
                self._emit(progress, stage="backup2_original", message=f"Backup 2 originals: {photo.original_name}", current=idx-1, total=len(selected), bytes_done=bytes_done, bytes_total=total_bytes, rate_bps=(bytes_done / max(0.001, time.monotonic()-start_time)))

                def add_bytes(n: int) -> None:
                    nonlocal bytes_done
                    bytes_done += n
                    self._emit(progress, stage="backup2_original", message=f"Backup 2 originals: {photo.original_name}", current=idx-1, total=len(selected), bytes_done=bytes_done, bytes_total=total_bytes, rate_bps=(bytes_done / max(0.001, time.monotonic()-start_time)))

                digest = self._copy_source_to_primary(src, dest, photo.size, add_bytes)
                manifest["selected_files"].append({
                    "source": photo.path,
                    "original_name": photo.original_name,
                    "number": photo.number,
                    "renamed": photo.renamed,
                    "size": photo.size,
                    "sha256": digest,
                })
                self.storage.save_manifest(job_id, manifest)

            self._verify_manifest_files(b2, manifest["selected_files"])
            self._verify_folder_count(b2, len(selected))
            manifest["backups"]["backup2"]["status"] = "verified"
            self._write_manifest_copy(b2, manifest)
            self.storage.save_manifest(job_id, manifest)
            self.storage.upsert_job(self._job_row(manifest, manifest_path))
            self._emit(progress, stage="backup2_ready", message="Backup 2 originals verified", current=len(selected), total=len(selected), bytes_done=total_bytes, bytes_total=total_bytes, rate_bps=0)

            # Backup 2 -> temporary exact-original staging.
            for i, item in enumerate(manifest["selected_files"], start=1):
                if self.cancel_event.is_set():
                    raise BackupError("Backup cancelled")
                src = b2 / item["renamed"]
                dest = staging / item["renamed"]
                self._copy_verified_file(src, dest, item["size"], item["sha256"])
                self._emit(progress, stage="staging", message=f"Temporary original safety: {item['renamed']}", current=i, total=len(selected), bytes_done=i, bytes_total=len(selected), rate_bps=0)

            self._verify_manifest_files(staging, manifest["selected_files"])
            self._verify_folder_count(staging, len(selected))
            manifest["backups"]["staging"]["status"] = "verified"
            self._write_manifest_copy(staging, manifest)

            self._verify_manifest_files(b2, manifest["selected_files"])
            self._verify_manifest_files(staging, manifest["selected_files"])
            if path_network_identity(b2) == path_network_identity(staging):
                raise BackupError("SD deletion blocked: Backup 2 and temporary originals are on the same drive/share")

            manifest["status"] = "originals_safe_local_dropbox_pending_autocorrect_pending"
            self.storage.save_manifest(job_id, manifest)
            self._write_manifest_copy(b2, manifest)
            self._write_manifest_copy(staging, manifest)
            self.storage.upsert_job(self._job_row(manifest, manifest_path))

            payload = self._event_payload(manifest)
            self.eventlog.queue(job_id, payload)
            sent, _ = self.eventlog.try_send_job(job_id)
            manifest["eventlog_status"] = "sent" if sent else "queued"
            self.storage.save_manifest(job_id, manifest)
            self.storage.upsert_job(self._job_row(manifest, manifest_path))

            self._emit(progress, stage="originals_safe", message="Originals protected twice — SD card can be cleared while Auto Correct and Dropbox continue", current=len(selected), total=len(selected), bytes_done=total_bytes, bytes_total=total_bytes, rate_bps=0)
            return manifest
        except Exception as exc:
            manifest["status"] = "interrupted"
            manifest["sd_deleted"] = False
            manifest["error"] = str(exc)
            self.storage.save_manifest(job_id, manifest)
            self.storage.upsert_job(self._job_row(manifest, manifest_path, str(exc)))
            self._emit(progress, stage="error", message=str(exc), current=0, total=len(selected), bytes_done=bytes_done, bytes_total=total_bytes, rate_bps=0)
            raise

    def build_autocorrect_backup1(self, job_id: str, progress: Callable[[ProgressEvent], None] | None = None) -> dict[str, Any]:
        manifest = self.storage.load_manifest(job_id)
        if not manifest or manifest.get("backup_mode") != "auto_correct":
            return manifest or {}
        backups = manifest.get("backups", {})
        if backups.get("backup2", {}).get("status") != "verified":
            raise BackupError("Auto Correct requires verified Backup 2 originals")

        source_folder = Path(backups["backup2"]["path"])
        dest_folder = Path(backups["backup1"]["path"])
        dest_folder.mkdir(parents=True, exist_ok=True)
        corrected = {x.get("renamed"): x for x in manifest.get("autocorrect_files", [])}
        files = manifest.get("selected_files", [])
        event_code = str(manifest.get("event", {}).get("event_code", ""))

        for idx, item in enumerate(files, start=1):
            if self.cancel_event.is_set():
                raise BackupError("Auto Correct cancelled")
            src = source_folder / item["renamed"]
            dest = dest_folder / item["renamed"]
            existing = corrected.get(item["renamed"])
            if existing and dest.exists() and dest.stat().st_size == existing.get("size") and sha256_file(dest) == existing.get("sha256"):
                self._emit(progress, stage="autocorrect", message=f"Auto Correct: {item['renamed']}", current=idx, total=len(files), bytes_done=idx, bytes_total=len(files), rate_bps=0)
                continue
            # Backup 1 is derived working data only. If a previous correction was
            # interrupted between file-save and manifest-save, safely rebuild that
            # working JPEG from untouched Backup 2 originals.
            if dest.exists():
                dest.unlink()

            result = auto_correct_jpeg(src, dest, event_code)
            corrected[item["renamed"]] = {
                "renamed": item["renamed"],
                "size": result["size"],
                "sha256": result["sha256"],
                "width": result["width"],
                "height": result["height"],
                "exposure_factor": result["exposure_factor"],
            }
            manifest["autocorrect_files"] = list(corrected.values())
            backups["backup1"]["status"] = "autocorrect_processing"
            self.storage.save_manifest(job_id, manifest)
            self._emit(progress, stage="autocorrect", message=f"Auto Correct: {item['renamed']}", current=idx, total=len(files), bytes_done=idx, bytes_total=len(files), rate_bps=0)

        self._verify_folder_count(dest_folder, len(files))
        for item in manifest.get("autocorrect_files", []):
            p = dest_folder / item["renamed"]
            if not p.exists() or p.stat().st_size != item["size"] or sha256_file(p) != item["sha256"]:
                raise BackupError(f"Auto Correct verification failed: {item['renamed']}")

        backups["backup1"]["status"] = "autocorrect_verified"
        backups["backup1"]["verified_at"] = datetime.now().isoformat()
        cloud_ok = backups.get("dropbox", {}).get("status") == "verified"
        manifest["status"] = "backup1_autocorrected_backup2_original_dropbox_verified" if cloud_ok else "backup1_autocorrected_backup2_original_dropbox_pending"
        self.storage.save_manifest(job_id, manifest)
        row = self.storage.get_job(job_id)
        manifest_path = Path(row["manifest_path"]) if row else self.storage.save_manifest(job_id, manifest)
        self.storage.upsert_job(self._job_row(manifest, manifest_path))
        self._write_manifest_copy(dest_folder, manifest)
        if source_folder.exists():
            self._write_manifest_copy(source_folder, manifest)
        return manifest

    def mark_autocorrect_failed(self, job_id: str, error: str) -> None:
        manifest = self.storage.load_manifest(job_id)
        if not manifest or manifest.get("backup_mode") != "auto_correct":
            return
        backups = manifest.setdefault("backups", {})
        b1 = backups.setdefault("backup1", {})
        b1["status"] = "autocorrect_failed"
        b1["last_error"] = error
        manifest["status"] = "backup2_original_safe_autocorrect_failed_dropbox_pending"
        manifest["autocorrect_error"] = error
        self.storage.save_manifest(job_id, manifest)
        row = self.storage.get_job(job_id)
        manifest_path = Path(row["manifest_path"]) if row else self.storage.save_manifest(job_id, manifest)
        self.storage.upsert_job(self._job_row(manifest, manifest_path))
        source_folder = Path(backups.get("backup2", {}).get("path", ""))
        if source_folder.exists():
            try:
                self._write_manifest_copy(source_folder, manifest)
            except Exception:
                pass

    def restore_backup1_originals(self, job_id: str, progress: Callable[[ProgressEvent], None] | None = None) -> tuple[dict[str, Any], str]:
        """Rebuild Backup 1 from untouched Backup 2 originals without deleting corrected files."""
        manifest = self.storage.load_manifest(job_id)
        if not manifest:
            raise BackupError("Manifest not found")
        backups = manifest.get("backups", {})
        if backups.get("backup2", {}).get("status") != "verified":
            raise BackupError("Verified Backup 2 originals are required for rollback")

        source_folder = Path(backups["backup2"]["path"])
        dest_folder = Path(backups["backup1"]["path"])
        archive_path = ""
        if dest_folder.exists() and any(dest_folder.iterdir()):
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            saved = dest_folder.with_name(dest_folder.name + f"-AUTO-CORRECTED-SAVED-{stamp}")
            if saved.exists():
                saved = dest_folder.with_name(dest_folder.name + f"-AUTO-CORRECTED-SAVED-{stamp}-{uuid.uuid4().hex[:4].upper()}")
            dest_folder.rename(saved)
            archive_path = str(saved)

        dest_folder.mkdir(parents=True, exist_ok=True)
        files = manifest.get("selected_files", [])
        for idx, item in enumerate(files, start=1):
            self._copy_verified_file(source_folder / item["renamed"], dest_folder / item["renamed"], item["size"], item["sha256"])
            self._emit(progress, stage="restore_backup1", message=f"Restore Backup 1 originals: {item['renamed']}", current=idx, total=len(files), bytes_done=idx, bytes_total=len(files), rate_bps=0)

        self._verify_manifest_files(dest_folder, files)
        self._verify_folder_count(dest_folder, len(files))
        manifest["backup_mode"] = "original_restored"
        manifest["autocorrect_archive_path"] = archive_path
        backups["backup1"]["status"] = "verified"
        backups["backup1"]["kind"] = "original_restored"
        cloud_ok = backups.get("dropbox", {}).get("status") == "verified"
        manifest["status"] = "restored_originals_dropbox_verified" if cloud_ok else "restored_originals_dropbox_pending"
        self.storage.save_manifest(job_id, manifest)
        row = self.storage.get_job(job_id)
        manifest_path = Path(row["manifest_path"]) if row else self.storage.save_manifest(job_id, manifest)
        self.storage.upsert_job(self._job_row(manifest, manifest_path))
        self._write_manifest_copy(dest_folder, manifest)
        self._write_manifest_copy(source_folder, manifest)
        return manifest, archive_path

    def run_emergency(self, spec: BackupJobSpec, progress: Callable[[ProgressEvent], None] | None = None) -> dict[str, Any]:
        self.reset_cancel()
        folder_name, photos = self._build_names(spec)
        selected = [p for p in photos if p.selected]
        if not selected:
            raise BackupError("No photos selected")
        total_bytes = sum(p.size for p in selected)
        emergency_root = self.storage.get_setting("emergency_path", "")
        ok, msg, _ = self.preflight_path(emergency_root, total_bytes)
        if not ok:
            raise BackupError(f"Emergency location unavailable: {msg}")
        dest_folder = Path(emergency_root) / folder_name
        dest_folder.mkdir(parents=True, exist_ok=True)

        job_id = self._make_job_id(spec)
        created = datetime.now().isoformat()
        manifest: dict[str, Any] = {
            "schema": 1,
            "job_id": job_id,
            "created_at": created,
            "source_path": spec.source_path,
            "folder_name": folder_name,
            "event": {
                "date": spec.date_text, "photographer": spec.photographer, "initials": spec.initials,
                "event_name": spec.event_name, "event_code": spec.event_code, "event_time": spec.event_time,
                "guests": spec.guests, "issue_type": spec.issue_type, "notes": spec.notes,
            },
            "excluded_count": len([p for p in photos if not p.selected]),
            "selected_files": [],
            "backups": {
                "backup1": {"path": "", "status": "pending_local"},
                "backup2": {"path": "", "status": "pending_local"},
                "dropbox": {"path": f"{DROPBOX_DESTINATION}/{folder_name}", "status": "pending", "uploaded": []},
                "emergency": {"path": str(dest_folder), "status": "running"},
            },
            "status": "emergency_running",
            "eventlog_status": "queued",
            "sd_deleted": False,
        }
        manifest_path = self.storage.save_manifest(job_id, manifest)
        self.storage.upsert_job(self._job_row(manifest, manifest_path))

        bytes_done = 0
        start = time.monotonic()
        try:
            for idx, photo in enumerate(selected, start=1):
                src = Path(photo.path)
                dest = dest_folder / photo.renamed

                def add_bytes(n: int) -> None:
                    nonlocal bytes_done
                    bytes_done += n
                    self._emit(progress, stage="emergency", message=f"Emergency backup: {photo.original_name}", current=idx-1, total=len(selected), bytes_done=bytes_done, bytes_total=total_bytes, rate_bps=(bytes_done/max(0.001,time.monotonic()-start)))

                digest = self._copy_source_to_primary(src, dest, photo.size, add_bytes)
                manifest["selected_files"].append({
                    "source": photo.path, "original_name": photo.original_name, "number": photo.number,
                    "renamed": photo.renamed, "size": photo.size, "sha256": digest,
                })
                self.storage.save_manifest(job_id, manifest)

            self._verify_folder_count(dest_folder, len(selected))
            manifest["backups"]["emergency"]["status"] = "verified"
            manifest["status"] = "emergency_verified_local_backups_pending"
            self._write_manifest_copy(dest_folder, manifest)
            self.storage.save_manifest(job_id, manifest)
            self.storage.upsert_job(self._job_row(manifest, manifest_path))
            self.eventlog.queue(job_id, self._event_payload(manifest))
            sent, _ = self.eventlog.try_send_job(job_id)
            manifest["eventlog_status"] = "sent" if sent else "queued"
            self.storage.save_manifest(job_id, manifest)
            self.storage.upsert_job(self._job_row(manifest, manifest_path))
            self._emit(progress, stage="emergency_complete", message="Emergency backup verified. Two normal local backups are still required.", current=len(selected), total=len(selected), bytes_done=total_bytes, bytes_total=total_bytes, rate_bps=0)
            return manifest
        except Exception as exc:
            manifest["status"] = "interrupted"
            manifest["sd_deleted"] = False
            manifest["error"] = str(exc)
            self.storage.save_manifest(job_id, manifest)
            self.storage.upsert_job(self._job_row(manifest, manifest_path, str(exc)))
            raise

    def complete_emergency_to_network(self, job_id: str, progress: Callable[[ProgressEvent], None] | None = None) -> dict[str, Any]:
        """Promote a verified emergency copy into the two required normal local backups."""
        self.reset_cancel()
        manifest = self.storage.load_manifest(job_id)
        if not manifest:
            raise BackupError("Manifest not found")
        if manifest["backups"]["emergency"]["status"] != "verified":
            raise BackupError("Emergency backup has not been verified")
        files = manifest["selected_files"]
        total_bytes = sum(x["size"] for x in files)
        checks = self.preflight_all(total_bytes)
        unavailable = [k for k, v in checks.items() if not v[0]]
        if unavailable:
            details = "; ".join(f"{k}: {checks[k][1]}" for k in unavailable)
            raise BackupError(details)

        source_folder = Path(manifest["backups"]["emergency"]["path"])
        folder_name = manifest["folder_name"]
        dests = {
            "backup1": Path(self.storage.get_setting("backup1_path")) / folder_name,
            "backup2": Path(self.storage.get_setting("backup2_path")) / folder_name,
        }
        for d in dests.values():
            d.mkdir(parents=True, exist_ok=True)

        try:
            for label, folder in dests.items():
                for i, item in enumerate(files, start=1):
                    src = source_folder / item["renamed"]
                    dest = folder / item["renamed"]
                    self._copy_verified_file(src, dest, item["size"], item["sha256"])
                    self._emit(progress, stage=label, message=f"Completing {label}: {item['renamed']}", current=i, total=len(files), bytes_done=i, bytes_total=len(files), rate_bps=0)
                self._verify_folder_count(folder, len(files))
                manifest["backups"][label] = {"path": str(folder), "status": "verified"}
                self._write_manifest_copy(folder, manifest)
                self.storage.save_manifest(job_id, manifest)
            manifest.setdefault("backups", {}).setdefault("dropbox", {"path": f"{DROPBOX_DESTINATION}/{folder_name}", "status": "pending", "uploaded": []})
            manifest["status"] = "verified_2_of_2_dropbox_pending"
            self.storage.save_manifest(job_id, manifest)
            row = self.storage.get_job(job_id)
            manifest_path = Path(row["manifest_path"]) if row else self.storage.save_manifest(job_id, manifest)
            self.storage.upsert_job(self._job_row(manifest, manifest_path))
            return manifest
        except Exception as exc:
            manifest["status"] = "interrupted"
            manifest["error"] = str(exc)
            self.storage.save_manifest(job_id, manifest)
            row = self.storage.get_job(job_id)
            manifest_path = Path(row["manifest_path"]) if row else self.storage.save_manifest(job_id, manifest)
            self.storage.upsert_job(self._job_row(manifest, manifest_path, str(exc)))
            raise

    def delete_verified_sources(self, manifest: dict[str, Any]) -> None:
        """Final deletion gate. Dropbox is deliberately NOT required to clear the SD card."""
        files = manifest.get("selected_files", [])
        if not files:
            raise BackupError("SD deletion blocked: no verified files in manifest")

        if manifest.get("backup_mode") == "auto_correct":
            backups = manifest.get("backups", {})
            for key in ("backup2", "staging"):
                info = backups.get(key, {})
                if info.get("status") != "verified":
                    raise BackupError(f"SD deletion blocked: {key} originals are not verified")
                folder = Path(info.get("path", ""))
                if not folder.exists():
                    raise BackupError(f"SD deletion blocked: connection lost to {key}")
                self._verify_manifest_files(folder, files)
                self._verify_folder_count(folder, len(files))
            if path_network_identity(Path(backups["backup2"]["path"])) == path_network_identity(Path(backups["staging"]["path"])):
                raise BackupError("SD deletion blocked: the two exact original copies are on the same drive/share")
        else:
            for key in ("backup1", "backup2"):
                info = manifest.get("backups", {}).get(key, {})
                if info.get("status") != "verified":
                    raise BackupError(f"SD deletion blocked: {key} is not verified")
                folder = Path(info.get("path", ""))
                if not folder.exists():
                    raise BackupError(f"SD deletion blocked: connection lost to {key}")
                self._verify_manifest_files(folder, files)
                self._verify_folder_count(folder, len(files))

        self._delete_selected_originals(manifest)

    def _delete_selected_originals(self, manifest: dict[str, Any]) -> None:
        # This method is called only after both required local backups have been verified.
        for item in manifest["selected_files"]:
            p = Path(item["source"])
            if p.exists():
                # Last source integrity check prevents deleting an altered/replaced card file.
                if p.stat().st_size != item["size"] or sha256_file(p) != item["sha256"]:
                    raise BackupError(f"Source changed since backup; refusing to delete {p.name}")
        for item in manifest["selected_files"]:
            p = Path(item["source"])
            if p.exists():
                p.unlink()
        remaining = [Path(item["source"]) for item in manifest["selected_files"] if Path(item["source"]).exists()]
        if remaining:
            raise BackupError(f"Could not delete {len(remaining)} imported source files")

    def _event_payload(self, manifest: dict[str, Any]) -> dict[str, Any]:
        e = manifest["event"]
        return {
            "import_id": manifest["job_id"],
            "date": e["date"],
            "photographer": e["photographer"],
            "photographer_initials": e["initials"],
            "event": e["event_name"],
            "event_code": e["event_code"],
            "event_time": e["event_time"],
            "quantity_type": "bikes" if str(e.get("event_code", "")).upper() == "ATV" else "guests",
            "number_of_bikes": e["guests"] if str(e.get("event_code", "")).upper() == "ATV" else None,
            "number_of_guests": None if str(e.get("event_code", "")).upper() == "ATV" else e["guests"],
            "number_of_photos": len(manifest["selected_files"]),
            "excluded_photos": manifest["excluded_count"],
            "issue_type": e.get("issue_type", "No issues"),
            "notes": e["notes"],
            "backup_status": manifest["status"],
            "primary_status": manifest["backups"]["backup1"]["status"],
            "backup2_status": manifest["backups"]["backup2"]["status"],
            "dropbox_status": manifest["backups"].get("dropbox", {"status": "pending"})["status"],
            "emergency_status": manifest["backups"]["emergency"]["status"],
            "folder_name": manifest["folder_name"],
        }
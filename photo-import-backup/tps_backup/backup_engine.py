from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Any

from .storage import Storage
from .eventlog import EventLogClient
from .dropbox_client import DROPBOX_DESTINATION

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
        self.machine_id = (os.environ.get("COMPUTERNAME") or socket.gethostname() or "UNKNOWN-PC").strip()

    def _log_stage(self, job_id: str, stage: str, status: str, message: str) -> None:
        try:
            self.storage.log_stage(
                job_id=job_id,
                machine_id=self.machine_id,
                stage=stage,
                status=status,
                message=message,
                ts=datetime.now().isoformat(),
            )
        except Exception:
            pass

    def _safety_cache_root(self) -> Path:
        raw = str(self.storage.get_setting("safety_cache_path", "") or "").strip()
        if not raw:
            raw = str(Path.home() / "TPS PHOTO SAFETY CACHE")
        return Path(raw)

    def _is_fixed_local_storage(self, path: Path) -> bool:
        raw = str(path)
        if raw.startswith("\\\\"):
            return False
        if os.name != "nt":
            return True
        try:
            import ctypes
            drive = path.drive
            if not drive:
                return False
            root = drive + "\\"
            DRIVE_FIXED = 3
            return int(ctypes.windll.kernel32.GetDriveTypeW(root)) == DRIVE_FIXED
        except Exception:
            return False

    def preflight_safety_cache(self, required_bytes: int) -> tuple[bool, str, int]:
        root = self._safety_cache_root()
        try:
            root.mkdir(parents=True, exist_ok=True)
            if not self._is_fixed_local_storage(root):
                return False, "Safety cache must be on this computer's fixed local drive", 0
            usage = shutil.disk_usage(root)
            # Leave a modest reserve so Windows is not filled by a large card.
            reserve = max(2 * 1024**3, required_bytes // 5)
            if usage.free < required_bytes + reserve:
                return False, f"Insufficient local safety-cache space ({usage.free:,} bytes free)", usage.free
            test = root / f".tps_cache_test_{uuid.uuid4().hex}.tmp"
            with test.open("wb") as handle:
                handle.write(b"TPS")
                handle.flush()
                os.fsync(handle.fileno())
            test.unlink()
            return True, "Ready", usage.free
        except Exception as exc:
            return False, f"Safety cache unavailable: {exc}", 0

    def _network_slot_dir(self) -> Path:
        base = Path(str(self.storage.get_setting("backup1_path", "") or ""))
        if not str(base):
            raise BackupError("Backup 1 is not configured")
        return base / ".TPS-IMPORT-SLOTS"

    def _acquire_network_slot(
        self,
        job_id: str,
        progress: Callable[[ProgressEvent], None] | None = None,
    ) -> Path:
        slot_dir = self._network_slot_dir()
        slot_dir.mkdir(parents=True, exist_ok=True)
        limit = int(self.storage.get_setting("network_concurrency_limit", 2) or 2)
        limit = max(1, min(6, limit))
        stale_seconds = 6 * 60 * 60

        while not self.cancel_event.is_set():
            for i in range(1, limit + 1):
                path = slot_dir / f"slot-{i}.lock"
                try:
                    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    payload = {
                        "job_id": job_id,
                        "machine_id": self.machine_id,
                        "started_at": datetime.now().isoformat(),
                    }
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        json.dump(payload, handle)
                        handle.flush()
                        os.fsync(handle.fileno())
                    self._log_stage(job_id, "network_slot", "acquired", f"Network slot {i}/{limit}")
                    return path
                except FileExistsError:
                    try:
                        if time.time() - path.stat().st_mtime > stale_seconds:
                            path.unlink(missing_ok=True)
                    except Exception:
                        pass
                except Exception as exc:
                    raise BackupError(f"Could not acquire shared network backup slot: {exc}") from exc

            self._emit(
                progress,
                stage="waiting_network",
                message=f"Local safety copy verified — waiting for a network slot (max {limit} simultaneous jobs)",
                current=0,
                total=0,
                bytes_done=0,
                bytes_total=0,
                rate_bps=0,
            )
            time.sleep(2)

        raise BackupError("Backup cancelled while waiting for network slot")

    def _release_lock(self, path: Path | None) -> None:
        if not path:
            return
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

    def _acquire_folder_lock(self, folder: Path, job_id: str) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        lock = folder / ".TPS-IMPORT-LOCK"
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 6 * 60 * 60:
                    lock.unlink(missing_ok=True)
                    fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                else:
                    detail = lock.read_text(encoding="utf-8", errors="replace")
                    raise BackupError(f"Another TPS computer is already using this event folder: {folder}. {detail}")
            except BackupError:
                raise
            except Exception as exc:
                raise BackupError(f"Event folder is locked by another import: {folder}") from exc
        payload = {
            "job_id": job_id,
            "machine_id": self.machine_id,
            "locked_at": datetime.now().isoformat(),
        }
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        return lock

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

    def preflight_all(self, required_bytes: int) -> dict[str, tuple[bool, str, int]]:
        """Preflight the two required local/filesystem backups. Dropbox never blocks card safety."""
        raw = {
            "backup1": self.storage.get_setting("backup1_path", ""),
            "backup2": self.storage.get_setting("backup2_path", ""),
        }
        results = {k: self.preflight_path(v, required_bytes) for k, v in raw.items()}
        # If both configured folders are on the same visible volume/share, ensure the
        # shared free space can hold both copies. Separate physical storage is still recommended.
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
        """Create a verified local safety copy first, then two sequential network copies.

        The SD card is never deleted here. The UI asks for a separate final confirmation,
        and delete_verified_sources() then re-hashes all three copies immediately before
        removing any original from the card.
        """
        self.reset_cancel()
        folder_name, photos = self._build_names(spec)
        selected = [p for p in photos if p.selected]
        if not selected:
            raise BackupError("No photos selected")

        total_bytes = sum(p.size for p in selected)
        cache_ok, cache_msg, _ = self.preflight_safety_cache(total_bytes)
        if not cache_ok:
            raise BackupError(cache_msg)

        job_id = self._make_job_id(spec)
        created = datetime.now().isoformat()
        cache_folder = self._safety_cache_root() / folder_name / job_id
        b1 = Path(str(self.storage.get_setting("backup1_path", "") or "")) / folder_name
        b2 = Path(str(self.storage.get_setting("backup2_path", "") or "")) / folder_name
        dropbox_status = "pending" if (
            self.storage.get_setting("dropbox_app_key", "")
            and self.storage.get_setting("dropbox_refresh_token", "")
        ) else "not_connected"

        manifest: dict[str, Any] = {
            "schema": 3,
            "job_id": job_id,
            "created_at": created,
            "machine_id": self.machine_id,
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
                "local_cache": {
                    "path": str(cache_folder),
                    "status": "pending",
                    "retain_days": int(self.storage.get_setting("safety_cache_retention_days", 7) or 7),
                },
                "backup1": {"path": str(b1), "status": "pending"},
                "backup2": {"path": str(b2), "status": "pending"},
                "dropbox": {
                    "path": f"{DROPBOX_DESTINATION}/{folder_name}",
                    "status": dropbox_status,
                    "uploaded": [],
                },
                "emergency": {"path": "", "status": "not_used"},
            },
            "status": "running",
            "eventlog_status": "queued",
            "sd_deleted": False,
        }
        manifest_path = self.storage.save_manifest(job_id, manifest)
        self.storage.upsert_job(self._job_row(manifest, manifest_path))
        self._log_stage(job_id, "job", "started", f"Import started on {self.machine_id}")

        network_slot: Path | None = None
        folder_locks: list[Path] = []
        bytes_done = 0
        start_time = time.monotonic()

        try:
            # 1) SD -> LOCAL FIXED-DISK SAFETY CACHE.
            cache_folder.mkdir(parents=True, exist_ok=True)
            self._log_stage(job_id, "local_cache", "started", str(cache_folder))
            for idx, photo in enumerate(selected, start=1):
                src = Path(photo.path)
                dest = cache_folder / photo.renamed
                self._emit(
                    progress,
                    stage="local_cache",
                    message=f"Safety cache: {photo.original_name}",
                    current=idx - 1,
                    total=len(selected),
                    bytes_done=bytes_done,
                    bytes_total=total_bytes,
                    rate_bps=(bytes_done / max(0.001, time.monotonic() - start_time)),
                )

                def add_bytes(n: int) -> None:
                    nonlocal bytes_done
                    bytes_done += n
                    self._emit(
                        progress,
                        stage="local_cache",
                        message=f"Safety cache: {photo.original_name}",
                        current=idx - 1,
                        total=len(selected),
                        bytes_done=bytes_done,
                        bytes_total=total_bytes,
                        rate_bps=(bytes_done / max(0.001, time.monotonic() - start_time)),
                    )

                digest = self._copy_source_to_primary(src, dest, photo.size, add_bytes)
                photo.sha256 = digest
                manifest["selected_files"].append({
                    "source": photo.path,
                    "original_name": photo.original_name,
                    "number": photo.number,
                    "renamed": photo.renamed,
                    "size": photo.size,
                    "sha256": digest,
                })
                self.storage.save_manifest(job_id, manifest)

            self._verify_folder_count(cache_folder, len(selected))
            self._verify_manifest_files(cache_folder, manifest["selected_files"])
            manifest["backups"]["local_cache"]["status"] = "verified"
            manifest["backups"]["local_cache"]["verified_at"] = datetime.now().isoformat()
            self._write_manifest_copy(cache_folder, manifest)
            self.storage.save_manifest(job_id, manifest)
            self.storage.upsert_job(self._job_row(manifest, manifest_path))
            self._log_stage(job_id, "local_cache", "verified", f"{len(selected)} files SHA-256 verified")
            self._emit(
                progress,
                stage="local_cache_ready",
                message="LOCAL SAFETY COPY VERIFIED ✓ — originals are protected on this PC; network backups are next",
                current=len(selected),
                total=len(selected),
                bytes_done=total_bytes,
                bytes_total=total_bytes,
                rate_bps=0,
            )

            # 2) Limit total network pressure across all TPS computers.
            network_slot = self._acquire_network_slot(job_id, progress)

            # 3) Once a network slot is available, check both destinations.
            checks = self.preflight_all(total_bytes)
            unavailable = [k for k, v in checks.items() if not v[0]]
            if unavailable:
                details = "; ".join(f"{k}: {checks[k][1]}" for k in unavailable)
                raise BackupError(
                    f"Network backup unavailable after local safety copy was secured. {details}. "
                    "The SD card has NOT been deleted and the local safety cache is intact."
                )

            for folder in (b1, b2):
                folder.mkdir(parents=True, exist_ok=True)
                folder_locks.append(self._acquire_folder_lock(folder, job_id))

            # 4) LOCAL CACHE -> BACKUP 1. Only one network destination is written at a time.
            self._log_stage(job_id, "backup1", "started", str(b1))
            for i, item in enumerate(manifest["selected_files"], start=1):
                src = cache_folder / item["renamed"]
                dest = b1 / item["renamed"]
                self._copy_verified_file(src, dest, item["size"], item["sha256"])
                self._emit(
                    progress,
                    stage="backup1",
                    message=f"Backup 1: {item['renamed']}",
                    current=i,
                    total=len(selected),
                    bytes_done=i,
                    bytes_total=len(selected),
                    rate_bps=0,
                )
            self._verify_folder_count(b1, len(selected))
            self._verify_manifest_files(b1, manifest["selected_files"])
            manifest["backups"]["backup1"]["status"] = "verified"
            manifest["backups"]["backup1"]["verified_at"] = datetime.now().isoformat()
            self._write_manifest_copy(b1, manifest)
            self.storage.save_manifest(job_id, manifest)
            self.storage.upsert_job(self._job_row(manifest, manifest_path))
            self._log_stage(job_id, "backup1", "verified", f"{len(selected)} files SHA-256 verified")

            # 5) LOCAL CACHE -> BACKUP 2, sequentially.
            self._log_stage(job_id, "backup2", "started", str(b2))
            for i, item in enumerate(manifest["selected_files"], start=1):
                src = cache_folder / item["renamed"]
                dest = b2 / item["renamed"]
                self._copy_verified_file(src, dest, item["size"], item["sha256"])
                self._emit(
                    progress,
                    stage="backup2",
                    message=f"Backup 2: {item['renamed']}",
                    current=i,
                    total=len(selected),
                    bytes_done=i,
                    bytes_total=len(selected),
                    rate_bps=0,
                )
            self._verify_folder_count(b2, len(selected))
            self._verify_manifest_files(b2, manifest["selected_files"])
            manifest["backups"]["backup2"]["status"] = "verified"
            manifest["backups"]["backup2"]["verified_at"] = datetime.now().isoformat()
            self._write_manifest_copy(b2, manifest)
            self.storage.save_manifest(job_id, manifest)
            self.storage.upsert_job(self._job_row(manifest, manifest_path))
            self._log_stage(job_id, "backup2", "verified", f"{len(selected)} files SHA-256 verified")

            # 6) Verify all three exact copies one more time before presenting the delete option.
            for key, folder in (("local_cache", cache_folder), ("backup1", b1), ("backup2", b2)):
                self._verify_folder_count(folder, len(selected))
                self._verify_manifest_files(folder, manifest["selected_files"])
                if manifest["backups"][key]["status"] != "verified":
                    raise BackupError(f"Final verification state is incomplete for {key}")

            manifest["status"] = "verified_safety_plus_2_network_dropbox_pending"
            manifest["pre_delete_verified_at"] = datetime.now().isoformat()
            self.storage.save_manifest(job_id, manifest)
            self._write_manifest_copy(cache_folder, manifest)
            self._write_manifest_copy(b1, manifest)
            self._write_manifest_copy(b2, manifest)
            self.storage.upsert_job(self._job_row(manifest, manifest_path))
            self._log_stage(job_id, "pre_delete_gate", "verified", "Local cache + Backup 1 + Backup 2 fully SHA-256 verified")

            payload = self._event_payload(manifest)
            payload["machine_id"] = self.machine_id
            payload["safety_cache_status"] = manifest["backups"]["local_cache"]["status"]
            self.eventlog.queue(job_id, payload)
            sent, _ = self.eventlog.try_send_job(job_id)
            manifest["eventlog_status"] = "sent" if sent else "queued"
            self.storage.save_manifest(job_id, manifest)
            self.storage.upsert_job(self._job_row(manifest, manifest_path))

            self._emit(
                progress,
                stage="complete",
                message="3 EXACT COPIES VERIFIED ✓ — local safety cache + Backup 1 + Backup 2",
                current=len(selected),
                total=len(selected),
                bytes_done=total_bytes,
                bytes_total=total_bytes,
                rate_bps=0,
            )
            return manifest

        except Exception as exc:
            manifest["status"] = "interrupted"
            manifest["sd_deleted"] = False
            manifest["error"] = str(exc)
            self.storage.save_manifest(job_id, manifest)
            self.storage.upsert_job(self._job_row(manifest, manifest_path, str(exc)))
            self._log_stage(job_id, "job", "error", str(exc))
            self._emit(
                progress,
                stage="error",
                message=str(exc),
                current=0,
                total=len(selected),
                bytes_done=bytes_done,
                bytes_total=total_bytes,
                rate_bps=0,
            )
            raise
        finally:
            for lock in reversed(folder_locks):
                self._release_lock(lock)
            self._release_lock(network_slot)

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

    def delete_verified_sources(
        self,
        manifest: dict[str, Any],
        progress: Callable[[ProgressEvent], None] | None = None,
    ) -> None:
        """Final SD deletion gate: re-hash all three exact copies before deleting anything."""
        files = manifest.get("selected_files", [])
        if not files:
            raise BackupError("SD deletion blocked: no verified files in manifest")

        job_id = manifest.get("job_id", "UNKNOWN")
        backups = manifest.get("backups", {})
        cache_info = backups.get("local_cache", {})
        if cache_info.get("status") != "verified":
            raise BackupError("SD deletion blocked: local safety cache is not verified")

        cache_folder = Path(cache_info.get("path", ""))
        if not cache_folder.exists():
            raise BackupError("SD deletion blocked: local safety cache is missing")

        network_slot: Path | None = None
        folder_locks: list[Path] = []
        try:
            network_slot = self._acquire_network_slot(job_id, progress)

            network_folders: list[tuple[str, Path]] = []
            for key in ("backup1", "backup2"):
                info = backups.get(key, {})
                if info.get("status") != "verified":
                    raise BackupError(f"SD deletion blocked: {key} is not verified")
                folder = Path(info.get("path", ""))
                if not folder.exists():
                    raise BackupError(f"SD deletion blocked: connection lost to {key}")
                network_folders.append((key, folder))
                folder_locks.append(self._acquire_folder_lock(folder, job_id))

            # Re-verify the local safety cache and both network destinations by full hash.
            verify_targets = [("Local safety cache", cache_folder)] + [
                ("Backup 1" if key == "backup1" else "Backup 2", folder)
                for key, folder in network_folders
            ]
            total_checks = len(files) * len(verify_targets)
            checked = 0
            for label, folder in verify_targets:
                self._verify_folder_count(folder, len(files))
                for item in files:
                    p = folder / item["renamed"]
                    if not p.exists():
                        raise BackupError(f"SD deletion blocked: {label} is missing {item['renamed']}")
                    if p.stat().st_size != item["size"]:
                        raise BackupError(f"SD deletion blocked: {label} size mismatch for {item['renamed']}")
                    if sha256_file(p) != item["sha256"]:
                        raise BackupError(f"SD deletion blocked: {label} SHA-256 mismatch for {item['renamed']}")
                    checked += 1
                    self._emit(
                        progress,
                        stage="final_verify",
                        message=f"Final safety check: {label} • {item['renamed']}",
                        current=checked,
                        total=total_checks,
                        bytes_done=checked,
                        bytes_total=total_checks,
                        rate_bps=0,
                    )

            # Verify every SD source still matches before the first delete occurs.
            for item in files:
                p = Path(item["source"])
                if not p.exists():
                    raise BackupError(f"SD deletion blocked: source file is missing before deletion: {p.name}")
                if p.stat().st_size != item["size"] or sha256_file(p) != item["sha256"]:
                    raise BackupError(f"Source changed since backup; refusing to delete {p.name}")

            manifest["final_delete_gate_verified_at"] = datetime.now().isoformat()
            manifest["sd_delete_state"] = "verified_ready_to_delete"
            self.storage.save_manifest(job_id, manifest)
            self._write_manifest_copy(cache_folder, manifest)
            for _, folder in network_folders:
                self._write_manifest_copy(folder, manifest)
            self._log_stage(
                job_id,
                "sd_delete_gate",
                "verified",
                "Local safety cache + Backup 1 + Backup 2 + SD originals all SHA-256 verified",
            )

            # Deletion is not filesystem-transactional, so persist progress after every file.
            # If Windows/card hardware fails midway, every deleted original already has 3 verified copies.
            manifest["sd_delete_state"] = "deleting"
            manifest["sd_deleted_files"] = []
            self.storage.save_manifest(job_id, manifest)
            for index, item in enumerate(files, start=1):
                p = Path(item["source"])
                if p.exists():
                    p.unlink()
                if p.exists():
                    raise BackupError(f"Could not delete source file: {p.name}")
                manifest["sd_deleted_files"].append(item["renamed"])
                self.storage.save_manifest(job_id, manifest)
                self._emit(
                    progress,
                    stage="sd_delete",
                    message=f"Clearing verified SD original: {item['original_name']}",
                    current=index,
                    total=len(files),
                    bytes_done=index,
                    bytes_total=len(files),
                    rate_bps=0,
                )

            remaining = [Path(item["source"]) for item in files if Path(item["source"]).exists()]
            if remaining:
                raise BackupError(f"Could not delete {len(remaining)} imported source files")

            manifest["sd_delete_state"] = "deleted"
            manifest["sd_deleted"] = True
            manifest["sd_deleted_at"] = datetime.now().isoformat()
            self.storage.save_manifest(job_id, manifest)
            self._log_stage(job_id, "sd_delete", "complete", f"{len(files)} SD originals deleted after three-copy verification")
        except Exception as exc:
            manifest["sd_delete_error"] = str(exc)
            manifest["sd_delete_state"] = manifest.get("sd_delete_state", "blocked")
            try:
                self.storage.save_manifest(job_id, manifest)
            except Exception:
                pass
            self._log_stage(job_id, "sd_delete", "error", str(exc))
            raise
        finally:
            for lock in reversed(folder_locks):
                self._release_lock(lock)
            self._release_lock(network_slot)

    def cleanup_expired_safety_cache(self) -> int:
        """Release only old caches whose SD originals were cleared and Dropbox is verified."""
        removed = 0
        now = datetime.now()
        for row in self.storage.list_jobs(limit=2000):
            if not row.get("sd_deleted"):
                continue
            if row.get("primary_status") != "verified" or row.get("backup2_status") != "verified":
                continue
            if row.get("backup3_status") != "verified":
                continue
            manifest = self.storage.load_manifest(row["id"])
            if not manifest:
                continue
            cache = manifest.get("backups", {}).get("local_cache", {})
            if cache.get("status") != "verified":
                continue
            try:
                created = datetime.fromisoformat(manifest.get("created_at", row.get("created_at", "")))
            except Exception:
                continue
            retain_days = int(cache.get("retain_days", self.storage.get_setting("safety_cache_retention_days", 7)) or 7)
            if now - created < timedelta(days=max(1, retain_days)):
                continue
            folder = Path(cache.get("path", ""))
            try:
                if folder.exists():
                    shutil.rmtree(folder)
                cache["status"] = "released_after_retention"
                cache["released_at"] = datetime.now().isoformat()
                self.storage.save_manifest(row["id"], manifest)
                self._log_stage(row["id"], "local_cache", "released", f"Released after {retain_days} day retention and verified Dropbox")
                removed += 1
                parent = folder.parent
                try:
                    if parent.exists() and not any(parent.iterdir()):
                        parent.rmdir()
                except Exception:
                    pass
            except Exception as exc:
                self._log_stage(row["id"], "local_cache", "cleanup_error", str(exc))
        return removed

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
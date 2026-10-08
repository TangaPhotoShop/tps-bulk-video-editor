from __future__ import annotations

import hashlib
import shutil
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Callable, Any

try:
    import dropbox
    from dropbox.common import PathRoot
    from dropbox.files import FileMetadata, FolderMetadata, WriteMode
    from dropbox.oauth import DropboxOAuth2FlowNoRedirect
    from dropbox.exceptions import ApiError
except Exception:  # pragma: no cover - build/runtime dependency guard
    dropbox = None
    PathRoot = None
    FileMetadata = FolderMetadata = WriteMode = None
    DropboxOAuth2FlowNoRedirect = None
    ApiError = Exception

from .storage import Storage

DROPBOX_DESTINATION = "/TPS/TOUR PHOTO BACKUPS/From TEMP CLOUD FOLDER"
DROPBOX_BLOCK = 4 * 1024 * 1024


class DropboxBackupError(RuntimeError):
    pass


def dropbox_content_hash(path: Path) -> str:
    """Dropbox content hash: SHA-256 of concatenated 4 MiB block SHA-256 digests."""
    overall = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(DROPBOX_BLOCK)
            if not block:
                break
            overall.update(hashlib.sha256(block).digest())
    return overall.hexdigest()


class DropboxBackupClient:
    def __init__(self, storage: Storage):
        self.storage = storage
        self._oauth_flow = None

    @property
    def destination(self) -> str:
        return DROPBOX_DESTINATION

    def configured(self) -> bool:
        return bool(self.storage.get_setting("dropbox_app_key", "").strip() and self.storage.get_setting("dropbox_refresh_token", "").strip())

    def connection_label(self) -> str:
        if not self.configured():
            return "Not connected"
        name = self.storage.get_setting("dropbox_account_name", "")
        email = self.storage.get_setting("dropbox_account_email", "")
        if name and email:
            return f"Connected • {name} ({email})"
        return "Connected"

    def start_oauth(self, app_key: str) -> str:
        if dropbox is None or DropboxOAuth2FlowNoRedirect is None:
            raise DropboxBackupError("Dropbox support is not installed in this build")
        app_key = (app_key or "").strip()
        if not app_key:
            raise DropboxBackupError("Enter the Dropbox App key first")
        self._oauth_flow = DropboxOAuth2FlowNoRedirect(
            app_key,
            token_access_type="offline",
            scope=["account_info.read", "files.metadata.read", "files.content.write"],
            use_pkce=True,
            timeout=120,
        )
        url = self._oauth_flow.start()
        webbrowser.open(url, new=2)
        return url

    def finish_oauth(self, code: str, app_key: str) -> tuple[str, str]:
        if not self._oauth_flow:
            raise DropboxBackupError("Start the Dropbox connection first")
        code = (code or "").strip()
        if not code:
            raise DropboxBackupError("No Dropbox authorization code was entered")
        try:
            result = self._oauth_flow.finish(code)
        finally:
            self._oauth_flow = None
        if not result.refresh_token:
            raise DropboxBackupError("Dropbox did not return an offline refresh token")
        self.storage.set_setting("dropbox_app_key", app_key.strip())
        self.storage.set_setting("dropbox_refresh_token", result.refresh_token)
        dbx = dropbox.Dropbox(
            oauth2_access_token=result.access_token,
            user_agent="TPSPhotoImportBackup/1.1.0",
            timeout=120,
        )
        acct = dbx.users_get_current_account()
        self.storage.set_setting("dropbox_account_name", acct.name.display_name)
        self.storage.set_setting("dropbox_account_email", acct.email)
        self.storage.set_setting("dropbox_root_namespace_id", acct.root_info.root_namespace_id)
        self._rooted_client(dbx=dbx, account=acct)  # validates path-root support
        return acct.name.display_name, acct.email

    def disconnect(self) -> None:
        for key, value in (
            ("dropbox_refresh_token", ""),
            ("dropbox_account_name", ""),
            ("dropbox_account_email", ""),
            ("dropbox_root_namespace_id", ""),
        ):
            self.storage.set_setting(key, value)

    def _client(self):
        if dropbox is None:
            raise DropboxBackupError("Dropbox support is not installed in this build")
        app_key = self.storage.get_setting("dropbox_app_key", "").strip()
        refresh = self.storage.get_setting("dropbox_refresh_token", "").strip()
        if not app_key or not refresh:
            raise DropboxBackupError("Dropbox is not connected")
        dbx = dropbox.Dropbox(
            oauth2_refresh_token=refresh,
            app_key=app_key,
            user_agent="TPSPhotoImportBackup/1.1.0",
            timeout=180,
            max_retries_on_error=4,
            max_retries_on_rate_limit=5,
            auto_content_hash=True,
        )
        acct = dbx.users_get_current_account()
        # Save these again in case the team/account root changed.
        self.storage.set_setting("dropbox_account_name", acct.name.display_name)
        self.storage.set_setting("dropbox_account_email", acct.email)
        self.storage.set_setting("dropbox_root_namespace_id", acct.root_info.root_namespace_id)
        return self._rooted_client(dbx=dbx, account=acct), acct

    def _rooted_client(self, dbx, account):
        root_id = account.root_info.root_namespace_id
        return dbx.with_path_root(PathRoot.root(root_id))

    def test_connection(self) -> tuple[bool, str]:
        try:
            dbx, acct = self._client()
            meta = dbx.files_get_metadata(DROPBOX_DESTINATION)
            if not isinstance(meta, FolderMetadata):
                return False, "Configured Dropbox destination is not a folder"
            return True, f"Connected as {acct.name.display_name} • destination accessible"
        except Exception as exc:
            return False, str(exc)

    def _remote_metadata(self, dbx, path: str):
        try:
            return dbx.files_get_metadata(path)
        except ApiError as exc:
            # Dropbox path/not_found errors are normal when a file/folder is not uploaded yet.
            try:
                err = exc.error
                if hasattr(err, "is_path") and err.is_path():
                    lookup = err.get_path()
                    if hasattr(lookup, "is_not_found") and lookup.is_not_found():
                        return None
            except Exception:
                pass
            # String fallback keeps compatibility across SDK minor versions.
            if "not_found" in str(exc).lower():
                return None
            raise

    def _ensure_event_folder(self, dbx, remote_folder: str) -> None:
        root_meta = self._remote_metadata(dbx, DROPBOX_DESTINATION)
        if root_meta is None:
            raise DropboxBackupError(
                f"Dropbox destination does not exist or is not accessible: {DROPBOX_DESTINATION}. "
                "The Dropbox app must use Full Dropbox access."
            )
        if not isinstance(root_meta, FolderMetadata):
            raise DropboxBackupError("Dropbox destination is not a folder")
        meta = self._remote_metadata(dbx, remote_folder)
        if meta is None:
            dbx.files_create_folder_v2(remote_folder, autorename=False)
        elif not isinstance(meta, FolderMetadata):
            raise DropboxBackupError(f"Dropbox event path is not a folder: {remote_folder}")

    def upload_job(self, job_id: str, progress: Callable[[int, int, str], None] | None = None) -> dict[str, Any]:
        manifest = self.storage.load_manifest(job_id)
        if not manifest:
            raise DropboxBackupError("Backup manifest not found")
        backups = manifest.get("backups", {})
        b1 = backups.get("backup1", {})
        b2 = backups.get("backup2", {})
        if manifest.get("backup_mode") == "auto_correct":
            staging = backups.get("staging", {})
            if b2.get("status") != "verified" or staging.get("status") != "verified":
                raise DropboxBackupError("Dropbox originals require verified Backup 2 and temporary original safety copy first")
            source_folder = Path(staging.get("path", ""))
            if not source_folder.exists():
                source_folder = Path(b2.get("path", ""))
            if not source_folder.exists():
                raise DropboxBackupError("No verified original source is reachable for Dropbox")
        else:
            if b1.get("status") != "verified" or b2.get("status") != "verified":
                raise DropboxBackupError("Dropbox upload requires both local backups to be verified first")
            source_folder = Path(b1.get("path", ""))
            if not source_folder.exists():
                raise DropboxBackupError("Primary verified backup is no longer reachable")

        dbx, _ = self._client()
        remote_folder = f"{DROPBOX_DESTINATION}/{manifest['folder_name']}"
        self._ensure_event_folder(dbx, remote_folder)
        cloud = backups.setdefault("dropbox", {"path": remote_folder, "status": "pending", "uploaded": []})
        cloud["path"] = remote_folder
        cloud["status"] = "uploading"
        uploaded = set(cloud.get("uploaded", []))
        self._save_cloud_state(manifest)

        files = manifest.get("selected_files", [])
        total = len(files)
        for idx, item in enumerate(files, start=1):
            local_path = source_folder / item["renamed"]
            if not local_path.exists() or local_path.stat().st_size != item["size"]:
                raise DropboxBackupError(f"Verified local source missing or wrong size: {item['renamed']}")
            dbx_hash = dropbox_content_hash(local_path)
            remote_path = f"{remote_folder}/{item['renamed']}"
            meta = self._remote_metadata(dbx, remote_path)
            if isinstance(meta, FileMetadata) and meta.size == item["size"] and meta.content_hash == dbx_hash:
                uploaded.add(item["renamed"])
            else:
                # JPEGs are comfortably below the simple-upload API limit in normal TPS use.
                # auto_content_hash=True means Dropbox also validates the content hash in transit.
                with local_path.open("rb") as f:
                    payload = f.read()
                meta = dbx.files_upload(
                    payload,
                    remote_path,
                    mode=WriteMode.overwrite,
                    autorename=False,
                    mute=True,
                    strict_conflict=False,
                )
                if not isinstance(meta, FileMetadata) or meta.size != item["size"] or meta.content_hash != dbx_hash:
                    raise DropboxBackupError(f"Dropbox verification failed: {item['renamed']}")
                uploaded.add(item["renamed"])
            cloud["uploaded"] = sorted(uploaded)
            self._save_cloud_state(manifest)
            if progress:
                progress(idx, total, item["renamed"])

        cloud["status"] = "verified"
        cloud["verified_at"] = datetime.now().isoformat()
        if manifest.get("backup_mode") == "auto_correct":
            b1_status = backups.get("backup1", {}).get("status", "")
            manifest["status"] = (
                "backup1_autocorrected_backup2_original_dropbox_verified"
                if b1_status == "autocorrect_verified"
                else "backup2_original_dropbox_verified_autocorrect_pending"
            )
        else:
            manifest["status"] = "verified_2_of_2_dropbox_verified"
        self._save_cloud_state(manifest)

        if manifest.get("backup_mode") == "auto_correct":
            staging = backups.get("staging", {})
            staging_path = Path(staging.get("path", ""))
            if staging.get("status") == "verified" and staging_path.exists():
                try:
                    shutil.rmtree(staging_path)
                    staging["status"] = "cleaned_after_dropbox"
                    staging["cleaned_at"] = datetime.now().isoformat()
                    self._save_cloud_state(manifest)
                except Exception as exc:
                    staging["cleanup_error"] = str(exc)
                    self._save_cloud_state(manifest)
        return manifest

    def _save_cloud_state(self, manifest: dict[str, Any], error: str | None = None) -> None:
        job_id = manifest["job_id"]
        self.storage.save_manifest(job_id, manifest)
        row = self.storage.get_job(job_id)
        if row:
            cloud = manifest.get("backups", {}).get("dropbox", {})
            row["backup3_status"] = cloud.get("status", "pending")
            row["status"] = manifest.get("status", row["status"])
            row["updated_at"] = datetime.now().isoformat()
            row["error_message"] = error
            self.storage.upsert_job(row)
        for key in ("backup1", "backup2", "staging"):
            info = manifest.get("backups", {}).get(key, {})
            folder = Path(info.get("path", ""))
            if info.get("status") in {"verified", "autocorrect_verified", "autocorrect_processing"} and folder.exists():
                try:
                    tmp = folder / "TPS-IMPORT-MANIFEST.json.tmp"
                    final = folder / "TPS-IMPORT-MANIFEST.json"
                    import json
                    tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                    tmp.replace(final)
                except Exception:
                    pass

    def mark_error(self, job_id: str, error: str) -> None:
        manifest = self.storage.load_manifest(job_id)
        if not manifest:
            return
        cloud = manifest.setdefault("backups", {}).setdefault("dropbox", {"path": "", "status": "pending", "uploaded": []})
        cloud["status"] = "pending"
        cloud["last_error"] = error
        if manifest.get("backup_mode") == "auto_correct":
            b1_ok = manifest.get("backups", {}).get("backup1", {}).get("status") == "autocorrect_verified"
            manifest["status"] = "backup1_autocorrected_backup2_original_dropbox_pending" if b1_ok else "originals_safe_local_dropbox_pending_autocorrect_pending"
        else:
            manifest["status"] = "verified_2_of_2_dropbox_pending"
        self._save_cloud_state(manifest, error)

    def pending_jobs(self, limit: int = 100) -> list[str]:
        pending = []
        for row in self.storage.list_jobs(limit=limit):
            if row.get("backup3_status") == "verified":
                continue
            manifest = self.storage.load_manifest(row["id"])
            if not manifest:
                continue
            backups = manifest.get("backups", {})
            if manifest.get("backup_mode") == "auto_correct":
                if backups.get("backup2", {}).get("status") == "verified" and backups.get("staging", {}).get("status") == "verified":
                    pending.append(row["id"])
            elif row.get("primary_status") == "verified" and row.get("backup2_status") == "verified":
                pending.append(row["id"])
        return pending
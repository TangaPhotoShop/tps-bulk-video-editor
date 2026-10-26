from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from tps_backup.backup_engine import BackupEngine, BackupError, BackupJobSpec, SourcePhoto, sha256_file
from tps_backup.storage import Storage, MANIFEST_DIR


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="tps-safety-") as td:
        root = Path(td)
        card = root / "card" / "DCIM"
        cache = root / "cache"
        backup1 = root / "backup1"
        backup2 = root / "backup2"
        card.mkdir(parents=True)
        cache.mkdir()
        backup1.mkdir()
        backup2.mkdir()

        source = card / "IMG_0123.JPG"
        payload = (b"TPS-SAFETY-TEST-" * 8192) + b"END"
        source.write_bytes(payload)

        storage = Storage(root / "test.db")
        storage.set_setting("safety_cache_path", str(cache))
        storage.set_setting("backup1_path", str(backup1))
        storage.set_setting("backup2_path", str(backup2))
        storage.set_setting("network_concurrency_limit", 2)
        storage.set_setting("dropbox_app_key", "")
        storage.set_setting("dropbox_refresh_token", "")

        engine = BackupEngine(storage)
        engine.eventlog.queue = lambda *args, **kwargs: None
        engine.eventlog.try_send_job = lambda *args, **kwargs: (True, "test")

        spec = BackupJobSpec(
            date_text="2026-10-27",
            photographer="Steve",
            initials="SW",
            event_name="ATV Quad Bikes",
            event_code="ATV",
            event_time="09:00",
            guests=4,
            issue_type="No issues",
            notes="",
            source_path=str(card.parent),
            photos=[
                SourcePhoto(
                    path=str(source),
                    original_name=source.name,
                    number="0123",
                    selected=True,
                    size=source.stat().st_size,
                )
            ],
        )

        manifest = engine.run_normal(spec)
        assert source.exists(), "run_normal must never delete the SD original"
        assert manifest["backups"]["local_cache"]["status"] == "verified"
        assert manifest["backups"]["backup1"]["status"] == "verified"
        assert manifest["backups"]["backup2"]["status"] == "verified"
        assert manifest["machine_id"]

        item = manifest["selected_files"][0]
        cache_file = Path(manifest["backups"]["local_cache"]["path"]) / item["renamed"]
        b1_file = Path(manifest["backups"]["backup1"]["path"]) / item["renamed"]
        b2_file = Path(manifest["backups"]["backup2"]["path"]) / item["renamed"]

        expected = item["sha256"]
        assert sha256_file(cache_file) == expected
        assert sha256_file(b1_file) == expected
        assert sha256_file(b2_file) == expected

        # Deliberately corrupt one network copy without changing its byte count.
        original_b2 = b2_file.read_bytes()
        b2_file.write_bytes(b"X" * len(original_b2))
        try:
            engine.delete_verified_sources(manifest)
        except BackupError as exc:
            assert "SHA-256 mismatch" in str(exc)
        else:
            raise AssertionError("SD deletion should have been blocked by a corrupted Backup 2")

        assert source.exists(), "SD original was deleted even though a backup hash was wrong"

        # Restore the verified network copy and prove the final gate now permits deletion.
        shutil.copyfile(cache_file, b2_file)
        assert sha256_file(b2_file) == expected
        engine.delete_verified_sources(manifest)
        assert not source.exists(), "Verified SD original was not deleted"
        assert cache_file.exists() and b1_file.exists() and b2_file.exists()
        assert sha256_file(cache_file) == expected
        assert sha256_file(b1_file) == expected
        assert sha256_file(b2_file) == expected

        stages = storage.get_stage_log(manifest["job_id"])
        assert any(x["stage"] == "local_cache" and x["status"] == "verified" for x in stages)
        assert any(x["stage"] == "sd_delete_gate" and x["status"] == "verified" for x in stages)

        try:
            (MANIFEST_DIR / f"{manifest['job_id']}.json").unlink(missing_ok=True)
        except Exception:
            pass

        print("TPS multi-computer safety smoke test OK")


if __name__ == "__main__":
    main()

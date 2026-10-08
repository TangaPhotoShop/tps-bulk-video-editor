from __future__ import annotations

import tempfile
import sys
from pathlib import Path

# Running this file directly sets sys.path to tests/. Add the app root explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

import tps_backup.backup_engine as be
from tps_backup.backup_engine import BackupEngine, BackupJobSpec, discover_photos, sha256_file
from tps_backup.storage import Storage


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        sd = root / "sd"
        dcim = sd / "DCIM" / "100NIKON"
        b1 = root / "backup1"
        b2 = root / "backup2"
        emergency = root / "emergency"
        for p in (dcim, b1, b2, emergency):
            p.mkdir(parents=True, exist_ok=True)

        for i, level in enumerate((85, 125, 175), start=1):
            im = Image.new("RGB", (320, 220), (level, min(255, level + 18), max(0, level - 15)))
            im.save(dcim / f"DSC_{4800+i}.JPG", quality=92)

        storage = Storage(db_path=root / "test.sqlite")
        storage.set_setting("backup1_path", str(b1))
        storage.set_setting("backup2_path", str(b2))
        storage.set_setting("emergency_path", str(emergency))
        storage.set_setting("backup1_mode", "auto_correct")
        storage.set_setting("eventlog_enabled", False)
        storage.set_setting("dropbox_app_key", "")
        storage.set_setting("dropbox_refresh_token", "")

        # The CI runner has one physical C: volume. Model the production requirement
        # (Backup 2 and temporary safety are separate storage identities) explicitly.
        original_identity = be.path_network_identity
        def fake_identity(path: Path) -> str:
            s = str(path).lower()
            if "backup2" in s:
                return "backup2-volume"
            if "emergency" in s or "_tps_temp_original_staging" in s:
                return "staging-volume"
            return original_identity(path)
        be.path_network_identity = fake_identity

        try:
            photos = discover_photos(sd)
            engine = BackupEngine(storage)
            spec = BackupJobSpec(
                date_text="2026-10-08",
                photographer="Steve",
                initials="SW",
                event_name="Desert Safari",
                event_code="SAND",
                event_time="09:15",
                guests=20,
                issue_type="No issues",
                notes="",
                source_path=str(sd),
                photos=photos,
            )

            manifest = engine.run_normal(spec)
            assert manifest["backup_mode"] == "auto_correct"
            assert manifest["backups"]["backup2"]["status"] == "verified"
            assert manifest["backups"]["staging"]["status"] == "verified"
            assert manifest["backups"]["backup1"]["status"] == "autocorrect_pending"
            assert not Path(manifest["backups"]["backup1"]["path"]).exists()

            # SD deletion is allowed only because two exact original copies exist.
            engine.delete_verified_sources(manifest)
            assert not list(dcim.glob("*.JPG"))

            # Working Backup 1 can be generated after the card is gone.
            manifest = engine.build_autocorrect_backup1(manifest["job_id"])
            assert manifest["backups"]["backup1"]["status"] == "autocorrect_verified"
            b1_folder = Path(manifest["backups"]["backup1"]["path"])
            b2_folder = Path(manifest["backups"]["backup2"]["path"])
            assert len(list(b1_folder.glob("*.JPG"))) == 3
            assert len(list(b2_folder.glob("*.JPG"))) == 3

            # Rollback is non-destructive: corrected set is preserved, Backup 1 returns
            # to exact originals copied from Backup 2.
            manifest, archived = engine.restore_backup1_originals(manifest["job_id"])
            assert manifest["backup_mode"] == "original_restored"
            assert archived and Path(archived).exists()
            for item in manifest["selected_files"]:
                assert sha256_file(Path(manifest["backups"]["backup1"]["path"]) / item["renamed"]) == item["sha256"]
                assert sha256_file(b2_folder / item["renamed"]) == item["sha256"]

            print("AUTO CORRECT MODE SAFETY + ROLLBACK TEST OK")
        finally:
            be.path_network_identity = original_identity


if __name__ == "__main__":
    main()

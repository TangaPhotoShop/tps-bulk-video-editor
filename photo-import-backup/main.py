import sys
import ctypes
from pathlib import Path


def packaged_self_test() -> int:
    """Validate that the compiled EXE actually contains the application package."""
    marker = Path.cwd() / "tps_photo_import_self_test.txt"
    try:
        import tps_backup
        import tps_backup.storage
        import tps_backup.eventlog
        import tps_backup.dropbox_client
        import tps_backup.backup_engine
        from tps_backup.ui import TPSApp

        required = [
            "on_close",
            "save_admin_settings",
            "admin_connect_dropbox",
            "start_pending_dropbox_sync",
            "admin_add_event",
        ]
        missing = [name for name in required if not hasattr(TPSApp, name)]
        if missing:
            raise RuntimeError(f"Missing UI methods: {missing}")
        marker.write_text(f"OK {getattr(tps_backup, '__version__', 'unknown')}", encoding="utf-8")
        return 0
    except Exception as exc:
        marker.write_text(f"FAILED: {type(exc).__name__}: {exc}", encoding="utf-8")
        return 17


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(packaged_self_test())

    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "TangaloomaPhotoShop.TPSPhotoImportBackup"
            )
        except Exception:
            pass

    from tps_backup.ui import TPSApp

    app = TPSApp()
    app.mainloop()

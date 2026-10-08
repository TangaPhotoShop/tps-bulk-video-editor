from __future__ import annotations

import ctypes
import hashlib
import os
import queue
import subprocess
import time
import sys
import threading
from datetime import date
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

try:
    from PIL import Image, ImageTk, ImageOps
except Exception:  # pragma: no cover
    Image = None
    ImageTk = None
    ImageOps = None

from .backup_engine import (
    BackupEngine,
    BackupError,
    BackupJobSpec,
    ProgressEvent,
    SourcePhoto,
    date_code,
    discover_photos,
    safe_time_code,
)
from . import __version__ as APP_VERSION
from .storage import Storage
from .dropbox_client import DropboxBackupClient, DROPBOX_DESTINATION, DropboxBackupError

BG = "#EEF3F4"
CARD = "#FFFFFF"
INK = "#17343B"
MUTED = "#637A80"
NAV = "#073F49"
NAV_2 = "#0A5865"
GOLD = "#0B7280"
GOLD_2 = "#1090A0"
GREEN = "#2F7D65"
RED = "#B44848"
ORANGE = "#B06A2B"
LINE = "#D4E0E3"
BLUE = "#0B7280"

ADMIN_CODE_SHA256 = hashlib.sha256(b"4115").hexdigest()


def human_bytes(value: int) -> str:
    n = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{value} B"


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "assets" / name


def windows_removable_drives() -> list[str]:
    """Return only Windows DRIVE_REMOVABLE volumes; never mapped/network/fixed disks."""
    if os.name != "nt":
        return []
    drives = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for i in range(26):
        if bitmask & (1 << i):
            root = f"{chr(65+i)}:\\"
            dtype = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
            if dtype == 2:  # DRIVE_REMOVABLE
                drives.append(root)
    return drives


def windows_volume_serial(root: str) -> str:
    """Stable-enough card identity so a different SD card using the same drive letter is detected."""
    if os.name != "nt":
        return root
    serial = ctypes.c_uint32()
    max_component = ctypes.c_uint32()
    flags = ctypes.c_uint32()
    volume_name = ctypes.create_unicode_buffer(261)
    filesystem_name = ctypes.create_unicode_buffer(261)
    try:
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root),
            volume_name,
            len(volume_name),
            ctypes.byref(serial),
            ctypes.byref(max_component),
            ctypes.byref(flags),
            filesystem_name,
            len(filesystem_name),
        )
        if ok:
            return f"{root}|{serial.value:08X}"
    except Exception:
        pass
    return root


def windows_camera_sd_candidates() -> dict[str, str]:
    """Conservative camera-card detection: removable + DCIM + at least one JPEG under DCIM."""
    candidates: dict[str, str] = {}
    for root in windows_removable_drives():
        try:
            dcim = Path(root) / "DCIM"
            if not dcim.is_dir():
                continue
            has_jpeg = False
            for p in dcim.rglob("*"):
                if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg"}:
                    has_jpeg = True
                    break
            if not has_jpeg:
                continue
            candidates[windows_volume_serial(root)] = root
        except Exception:
            continue
    return candidates

def windows_eject_removable_drive(root: str) -> tuple[bool, str]:
    """Ask Windows Explorer to safely eject a removable volume and verify it disappears."""
    if os.name != "nt":
        return False, "Automatic eject is only available on Windows."
    root = (root or "").strip()
    if len(root) < 2 or root[1] != ":":
        return False, "Source is not a Windows drive letter."
    root = root[:2].upper() + "\\"

    try:
        dtype = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
        if dtype != 2:  # DRIVE_REMOVABLE only
            return False, "Windows does not identify this source as removable media."
    except Exception as exc:
        return False, f"Could not confirm removable media: {exc}"

    drive = root[:2]
    script = (
        "$ErrorActionPreference='Stop'; "
        f"$drive='{drive}'; "
        "$shell=New-Object -ComObject Shell.Application; "
        "$item=$shell.Namespace(17).ParseName($drive); "
        "if($null -eq $item){exit 2}; "
        "$item.InvokeVerb('Eject'); "
        "Start-Sleep -Milliseconds 500"
    )
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
            timeout=10,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except Exception as exc:
        return False, f"Windows eject command failed: {exc}"

    # Eject is asynchronous. Only report success when the mounted card really disappears.
    for _ in range(24):
        if not Path(root).exists():
            return True, f"{drive} safely ejected"
        time.sleep(0.2)
    return False, "Windows did not confirm that the SD card was ejected."


def windows_keep_awake(enable: bool) -> None:
    if os.name != "nt":
        return
    try:
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED if enable else ES_CONTINUOUS
        )
    except Exception:
        pass



class TPSApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TPS Photo Import & Backup")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(1480, max(1120, sw - 40))
        h = min(900, max(720, sh - 90))
        self.geometry(f"{w}x{h}")
        self.minsize(1120, 720)
        self.configure(bg=BG)
        self.storage = Storage()
        self.engine = BackupEngine(self.storage)
        self.dropbox = DropboxBackupClient(self.storage)
        self.dropbox_thread = None
        self.autocorrect_thread = None
        self.msg_queue: queue.Queue = queue.Queue()
        self.current_page = "import"
        self.photo_image = None
        self.preview_photo_path = None
        self.preview_resize_after = None
        self.thumbnail_images = {}
        self.thumbnail_generation = 0
        self.admin_unlocked = False
        self.backup_in_progress = False
        self.known_sd_cards: set[str] = set()
        self.history_sort_column = "date"
        self.history_sort_reverse = True
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        ico = resource_path("tps_photo_backup.ico")
        png_icon = resource_path("tps_photo_backup.png")
        try:
            if ico.exists():
                self.iconbitmap(str(ico))
        except Exception:
            pass
        try:
            if Image and ImageTk and png_icon.exists():
                icon_im = Image.open(png_icon).convert("RGBA")
                self.window_icon_image = ImageTk.PhotoImage(icon_im)
                self.iconphoto(True, self.window_icon_image)
        except Exception:
            pass

        self._style()
        self._layout()
        self.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self.after(300, self.poll_queue)
        self.after(700, lambda: self.monitor_sd_cards(initial=True))
        self.after(1000, self.auto_check_connections_on_load)
        self.after(1800, self.start_pending_dropbox_sync)
        self.after(2600, self.auto_sync_eventlog_queue)

    def _style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=INK, font=("Segoe UI", 10))
        style.configure("Card.TLabel", background=CARD, foreground=INK, font=("Segoe UI", 10))
        style.configure("Muted.Card.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Title.TLabel", background=BG, foreground=INK, font=("Segoe UI Semibold", 22))
        style.configure("Subtitle.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 10))
        style.configure("CardTitle.TLabel", background=CARD, foreground=INK, font=("Segoe UI Semibold", 13))
        style.configure("Gold.TLabel", background=CARD, foreground=GOLD, font=("Segoe UI Semibold", 10))
        style.configure("TEntry", padding=8, fieldbackground="#FBFAF7", bordercolor=LINE)
        style.configure("TCombobox", padding=7, fieldbackground="#FBFAF7")
        style.configure("Primary.TButton", font=("Segoe UI Semibold", 11), padding=(18, 12), foreground="#FFFFFF", background=GOLD, borderwidth=0)
        style.map("Primary.TButton", background=[("active", GOLD_2), ("disabled", "#CABFAD")], foreground=[("disabled", "#EFE9DE")])
        style.configure("Secondary.TButton", font=("Segoe UI Semibold", 10), padding=(12, 9), foreground=INK, background="#E9E3D8", borderwidth=0)
        style.map("Secondary.TButton", background=[("active", "#DDD4C6")])
        style.configure("Danger.TButton", font=("Segoe UI Semibold", 10), padding=(12, 9), foreground="#FFFFFF", background=RED, borderwidth=0)
        style.map("Danger.TButton", background=[("active", "#983737")])
        style.configure("Treeview", background="#FFFFFF", fieldbackground="#FFFFFF", foreground=INK, rowheight=30, bordercolor=LINE, font=("Segoe UI", 9))
        style.configure("Photo.Treeview", background="#FFFFFF", fieldbackground="#FFFFFF", foreground=INK, rowheight=58, bordercolor=LINE, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background="#EDE8DE", foreground=INK, font=("Segoe UI Semibold", 9), relief="flat")
        style.map("Treeview", background=[("selected", "#D7E6EA")], foreground=[("selected", INK)])
        style.configure("TCheckbutton", background=CARD, foreground=INK, font=("Segoe UI", 9))
        style.configure("Horizontal.TProgressbar", troughcolor="#E9E3D8", background=GOLD, borderwidth=0, thickness=14)

    def _layout(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.nav = tk.Frame(self, bg=NAV, width=235)
        self.nav.grid(row=0, column=0, sticky="nsw")
        self.nav.grid_propagate(False)
        self.content = tk.Frame(self, bg=BG)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

        brand = tk.Frame(self.nav, bg=NAV)
        brand.pack(fill="x", padx=22, pady=(24, 34))
        try:
            if Image and ImageTk:
                mark = Image.open(resource_path("tps_photo_backup.png"))
                mark.thumbnail((54, 54))
                self.brand_image = ImageTk.PhotoImage(mark)
                tk.Label(brand, image=self.brand_image, bg=NAV).pack(side="left")
            else:
                tk.Label(brand, text="TPS", bg=GOLD, fg=NAV, font=("Segoe UI Black", 14), width=4, height=2).pack(side="left")
        except Exception:
            tk.Label(brand, text="TPS", bg=GOLD, fg=NAV, font=("Segoe UI Black", 14), width=4, height=2).pack(side="left")
        tk.Label(brand, text="PHOTO IMPORT\n& BACKUP", bg=NAV, fg="#FFFFFF", justify="left", font=("Segoe UI Semibold", 11)).pack(side="left", padx=12)

        self.nav_buttons = {}
        for key, label, glyph in [
            ("import", "Import Event", "▣"),
            ("history", "Backup History", "◷"),
            ("admin", "Admin", "⚙"),
        ]:
            b = tk.Button(
                self.nav, text=f"  {glyph}   {label}", anchor="w",
                bg=NAV, fg="#D8E2E4", activebackground=NAV_2, activeforeground="#FFFFFF",
                bd=0, font=("Segoe UI Semibold", 10), padx=18, pady=14,
                command=lambda k=key: self.show_page(k)
            )
            b.pack(fill="x", padx=10, pady=3)
            self.nav_buttons[key] = b

        tk.Frame(self.nav, bg=NAV).pack(expand=True, fill="both")
        tk.Label(self.nav, text=f"VERSION {APP_VERSION}\nWindows desktop edition\n2 local verified • Dropbox cloud • Offline-safe", bg=NAV, fg="#8FA6AD", justify="left", font=("Segoe UI", 8)).pack(anchor="w", padx=22, pady=20)

        self.pages = {}
        for key in ("import", "history", "admin"):
            frame = tk.Frame(self.content, bg=BG)
            frame.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = frame
        self.build_import_page()
        self.build_history_page()
        self.build_admin_page()
        self.show_page("import")

    def _on_mousewheel(self, event):
        """Scroll the pane under the pointer and bubble to Admin when a list hits its edge."""
        try:
            widget = self.winfo_containing(self.winfo_pointerx(), self.winfo_pointery())
            if widget is None:
                return
            units = -1 if event.delta > 0 else 1

            # Prefer the list/table directly under the pointer, but only if it can
            # actually move in the requested direction.
            current = widget
            while current is not None:
                if isinstance(current, ttk.Treeview):
                    first, last = current.yview()
                    can_move = (units < 0 and first > 0.0) or (units > 0 and last < 1.0)
                    if can_move:
                        current.yview_scroll(units * 3, "units")
                        return "break"
                    break
                current = getattr(current, "master", None)

            # Admin is a vertically scrolling page. Wheel anywhere over Admin
            # content scrolls the page when an inner list cannot move further.
            if self.current_page == "admin" and hasattr(self, "admin_canvas"):
                current = widget
                while current is not None:
                    if current is getattr(self, "admin_canvas", None) or current is getattr(self, "admin_inner", None):
                        self.admin_canvas.yview_scroll(units * 3, "units")
                        return "break"
                    current = getattr(current, "master", None)
        except Exception:
            return

    def show_page(self, key: str):
        # Admin access is deliberately session-scoped to the Admin page only.
        # Leaving Admin immediately locks it again, so the 4115 code is required
        # every time someone comes back to Admin.
        if self.current_page == "admin" and key != "admin":
            self.admin_unlocked = False

        if key == "admin" and not self.admin_unlocked:
            code = simpledialog.askstring("Admin access", "Enter admin code", show="•", parent=self)
            if not code or hashlib.sha256(code.encode()).hexdigest() != ADMIN_CODE_SHA256:
                if code:
                    messagebox.showerror("Access denied", "Incorrect admin code.", parent=self)
                return
            self.admin_unlocked = True
            self.load_admin_settings()

        self.current_page = key
        self.pages[key].tkraise()
        for k, b in self.nav_buttons.items():
            b.configure(bg=NAV_2 if k == key else NAV, fg="#FFFFFF" if k == key else "#D8E2E4")
        if key == "history":
            self.refresh_history()

    # ---------------- Import page ----------------
    def build_import_page(self):
        page = self.pages["import"]
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(2, weight=1)

        head = tk.Frame(page, bg=BG)
        head.grid(row=0, column=0, sticky="ew", padx=34, pady=(25, 14))
        ttk.Label(head, text="Import Event", style="Title.TLabel").pack(anchor="w")
        tk.Label(head, text=f"Version {APP_VERSION}", bg=BG, fg="#7D9299", font=("Segoe UI Semibold", 8)).pack(anchor="w", pady=(1, 0))
        ttk.Label(head, text="Select the event, review the card, then create two exact local backups. Dropbox continues in the background.", style="Subtitle.TLabel").pack(anchor="w", pady=(3, 0))

        self.status_banner = tk.Frame(page, bg="#E7EEE9", highlightbackground="#C5D7CE", highlightthickness=1)
        self.status_banner.grid(row=1, column=0, sticky="ew", padx=34, pady=(0, 14))
        self.status_dot = tk.Label(self.status_banner, text="●", bg="#E7EEE9", fg=GREEN, font=("Segoe UI", 11))
        self.status_dot.pack(side="left", padx=(14, 8), pady=10)
        self.status_text = tk.Label(self.status_banner, text="Ready — insert an SD card or choose a source folder.", bg="#E7EEE9", fg=INK, font=("Segoe UI Semibold", 9))
        self.status_text.pack(side="left", pady=10)

        body = tk.Frame(page, bg=BG)
        body.grid(row=2, column=0, sticky="nsew", padx=34, pady=(0, 20))
        body.grid_columnconfigure(0, weight=4)
        body.grid_columnconfigure(1, weight=3)
        body.grid_rowconfigure(1, weight=1)

        # Event card
        event = tk.Frame(body, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        event.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 14))
        event.grid_columnconfigure(7, weight=1)
        tk.Label(event, text="EVENT DETAILS", bg=CARD, fg=GOLD, font=("Segoe UI Semibold", 9)).grid(row=0, column=0, columnspan=8, sticky="w", padx=18, pady=(14, 6))

        self.date_var = tk.StringVar(value=date.today().isoformat())
        self.photographer_var = tk.StringVar()
        self.initials_var = tk.StringVar()
        self.event_var = tk.StringVar()
        self.time_var = tk.StringVar()
        self.guests_var = tk.StringVar()
        self.issue_var = tk.StringVar(value="No issues")
        self.notes_var = tk.StringVar()

        fields = [
            ("Date", self.date_var, 0),
            ("Photographer", self.photographer_var, 2),
            ("Initials", self.initials_var, 4),
            ("Event", self.event_var, 6),
        ]
        for label, var, col in fields:
            tk.Label(event, text=label, bg=CARD, fg=MUTED, font=("Segoe UI", 8)).grid(row=1, column=col, sticky="w", padx=(18 if col == 0 else 8, 4))
            if label == "Photographer":
                self.photographer_combo = ttk.Combobox(event, textvariable=var, state="readonly", width=18)
                self.photographer_combo.grid(row=2, column=col, columnspan=2, sticky="ew", padx=(18 if col == 0 else 8, 8), pady=(2, 12))
                self.photographer_combo.bind("<<ComboboxSelected>>", lambda e: self.on_photographer_change())
            elif label == "Event":
                self.event_combo = ttk.Combobox(event, textvariable=var, state="readonly", width=20)
                self.event_combo.grid(row=2, column=col, columnspan=2, sticky="ew", padx=(8, 18), pady=(2, 12))
                self.event_combo.bind("<<ComboboxSelected>>", lambda e: self.on_event_change())
            else:
                ent = ttk.Entry(event, textvariable=var, width=16, state="readonly" if label == "Initials" else "normal")
                ent.grid(row=2, column=col, columnspan=2, sticky="ew", padx=(18 if col == 0 else 8, 8), pady=(2, 12))

        tk.Label(event, text="Event time", bg=CARD, fg=MUTED, font=("Segoe UI", 8)).grid(row=3, column=0, sticky="w", padx=18)
        self.time_combo = ttk.Combobox(event, textvariable=self.time_var, state="normal", width=16)
        self.time_combo.grid(row=4, column=0, columnspan=2, sticky="ew", padx=(18, 8), pady=(2, 14))
        self.quantity_label = tk.Label(event, text="Number of guests", bg=CARD, fg=MUTED, font=("Segoe UI", 8))
        self.quantity_label.grid(row=3, column=2, sticky="w", padx=8)
        ttk.Entry(event, textvariable=self.guests_var, width=14).grid(row=4, column=2, columnspan=2, sticky="ew", padx=8, pady=(2, 14))
        tk.Label(event, text="Anything to report?", bg=CARD, fg=MUTED, font=("Segoe UI", 8)).grid(row=3, column=4, sticky="w", padx=8)
        self.issue_combo = ttk.Combobox(
            event,
            textvariable=self.issue_var,
            state="readonly",
            values=[
                "No issues",
                "Camera issue",
                "Lens issue",
                "Flash issue",
                "Battery issue",
                "Memory card issue",
                "Radio / communications issue",
                "Other issue",
            ],
            width=24,
        )
        self.issue_combo.grid(row=4, column=4, columnspan=2, sticky="ew", padx=8, pady=(2, 14))
        self.issue_combo.bind("<<ComboboxSelected>>", lambda e: self.on_issue_change())
        self.notes_label = tk.Label(event, text="Notes (optional)", bg=CARD, fg=MUTED, font=("Segoe UI", 8))
        self.notes_label.grid(row=3, column=6, sticky="w", padx=8)
        self.notes_entry = ttk.Entry(event, textvariable=self.notes_var)
        self.notes_entry.grid(row=4, column=6, columnspan=2, sticky="ew", padx=(8, 18), pady=(2, 14))

        self.load_reference_data()
        self.on_issue_change()

        # Photo card left
        photos_card = tk.Frame(body, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        photos_card.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        photos_card.grid_columnconfigure(0, weight=1)
        photos_card.grid_rowconfigure(3, weight=1)

        top = tk.Frame(photos_card, bg=CARD)
        top.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 8))
        top.grid_columnconfigure(1, weight=1)
        tk.Label(top, text="PHOTO SELECTION", bg=CARD, fg=GOLD, font=("Segoe UI Semibold", 9)).grid(row=0, column=0, sticky="w")
        self.photo_summary = tk.Label(top, text="No card selected", bg=CARD, fg=MUTED, font=("Segoe UI", 9))
        self.photo_summary.grid(row=0, column=1, sticky="e")

        sourcebar = tk.Frame(photos_card, bg=CARD)
        sourcebar.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 8))
        sourcebar.grid_columnconfigure(0, weight=1)
        self.source_var = tk.StringVar()
        ttk.Entry(sourcebar, textvariable=self.source_var).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(sourcebar, text="Browse", style="Secondary.TButton", command=self.browse_source).grid(row=0, column=1)
        ttk.Button(sourcebar, text="Scan", style="Secondary.TButton", command=self.scan_source).grid(row=0, column=2, padx=(6, 0))

        actions = tk.Frame(photos_card, bg=CARD)
        actions.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))
        ttk.Button(actions, text="Select all", style="Secondary.TButton", command=lambda: self.set_all_selected(True)).pack(side="left")
        ttk.Button(actions, text="Deselect all", style="Secondary.TButton", command=lambda: self.set_all_selected(False)).pack(side="left", padx=6)
        ttk.Button(actions, text="Invert", style="Secondary.TButton", command=self.invert_selection).pack(side="left")
        ttk.Button(actions, text="Exclude selected", style="Secondary.TButton", command=self.exclude_tree_selection).pack(side="left", padx=6)

        tree_wrap = tk.Frame(photos_card, bg=CARD)
        tree_wrap.grid(row=3, column=0, sticky="nsew", padx=16, pady=(0, 10))
        tree_wrap.grid_columnconfigure(0, weight=1)
        tree_wrap.grid_rowconfigure(0, weight=1)
        self.photo_tree = ttk.Treeview(
            tree_wrap,
            columns=("include", "original", "number", "size"),
            show=("tree", "headings"),
            selectmode="extended",
            style="Photo.Treeview",
        )
        self.photo_tree.heading("#0", text="Preview")
        self.photo_tree.column("#0", width=68, minwidth=68, stretch=False, anchor="center")
        for col, text, width in [("include", "Import", 65), ("original", "Original filename", 190), ("number", "No.", 70), ("size", "Size", 80)]:
            self.photo_tree.heading(col, text=text)
            self.photo_tree.column(col, width=width, anchor="center" if col != "original" else "w")
        self.photo_tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.photo_tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.photo_tree.configure(yscrollcommand=sb.set)
        self.photo_tree.bind("<Double-1>", self.toggle_row)
        self.photo_tree.bind("<<TreeviewSelect>>", self.preview_selected)

        rangebar = tk.Frame(photos_card, bg=CARD)
        rangebar.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 14))
        tk.Label(rangebar, text="Quick range", bg=CARD, fg=MUTED, font=("Segoe UI", 8)).pack(side="left")
        self.range_start = tk.StringVar()
        self.range_end = tk.StringVar()
        ttk.Entry(rangebar, textvariable=self.range_start, width=8).pack(side="left", padx=(8, 4))
        tk.Label(rangebar, text="to", bg=CARD, fg=MUTED, font=("Segoe UI", 8)).pack(side="left")
        ttk.Entry(rangebar, textvariable=self.range_end, width=8).pack(side="left", padx=4)
        ttk.Button(rangebar, text="Select range", style="Secondary.TButton", command=self.select_range).pack(side="left", padx=6)

        # Right: preview + backup readiness
        right = tk.Frame(body, bg=BG)
        right.grid(row=1, column=1, sticky="nsew", padx=(8, 0))
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=1, minsize=330)

        preview = tk.Frame(right, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        preview.grid(row=0, column=0, sticky="nsew", pady=(0, 12))
        preview.grid_columnconfigure(0, weight=1)
        preview.grid_rowconfigure(1, weight=1)
        tk.Label(preview, text="PREVIEW", bg=CARD, fg=GOLD, font=("Segoe UI Semibold", 9)).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 8))
        self.preview_canvas = tk.Canvas(preview, bg="#1C252A", highlightthickness=0, height=330)
        self.preview_canvas.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 10))
        self.preview_canvas.bind("<Configure>", self._on_preview_resize)
        self.preview_canvas.create_text(10, 10, anchor="nw", text="Select a photo to preview", fill="#9AA7AC", font=("Segoe UI", 10))
        self.preview_name = tk.Label(preview, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 8))
        self.preview_name.grid(row=2, column=0, sticky="w", padx=16, pady=(0, 12))

        backup = tk.Frame(right, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        backup.grid(row=1, column=0, sticky="ew")
        backup.grid_columnconfigure(0, weight=1)
        tk.Label(backup, text="BACKUP READINESS", bg=CARD, fg=GOLD, font=("Segoe UI Semibold", 9)).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 8))
        self.backup_status_labels = {}
        self.backup_name_labels = {}
        for i, label in enumerate(("Backup 1", "Backup 2", "Dropbox cloud"), start=1):
            row = tk.Frame(backup, bg=CARD)
            row.grid(row=i, column=0, sticky="ew", padx=16, pady=2)
            name_label = tk.Label(row, text=label, bg=CARD, fg=INK, font=("Segoe UI Semibold", 9))
            name_label.pack(side="left")
            status = tk.Label(row, text="Not checked", bg=CARD, fg=MUTED, font=("Segoe UI", 9))
            status.pack(side="right")
            self.backup_name_labels[i] = name_label
            self.backup_status_labels[i] = status
        ttk.Button(backup, text="Check network now", style="Secondary.TButton", command=self.check_network).grid(row=4, column=0, sticky="ew", padx=16, pady=(10, 12))

        self.delete_var = tk.BooleanVar(value=bool(self.storage.get_setting("delete_after_verified_default", False)))
        ttk.Checkbutton(backup, text="Delete imported files from SD after 2/2 local verification", variable=self.delete_var).grid(row=5, column=0, sticky="w", padx=16, pady=(0, 10))

        self.folder_preview = tk.Label(backup, text="Folder preview: —", bg=CARD, fg=MUTED, anchor="w", justify="left", font=("Consolas", 8))
        self.folder_preview.grid(row=6, column=0, sticky="ew", padx=16, pady=(0, 10))

        self.progress = ttk.Progressbar(backup, mode="determinate", style="Horizontal.TProgressbar")
        self.progress.grid(row=7, column=0, sticky="ew", padx=16, pady=(0, 6))
        self.progress_text = tk.Label(backup, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 8))
        self.progress_text.grid(row=8, column=0, sticky="w", padx=16)

        btns = tk.Frame(backup, bg=CARD)
        btns.grid(row=9, column=0, sticky="ew", padx=16, pady=(12, 16))
        btns.grid_columnconfigure(0, weight=1)
        btns.grid_columnconfigure(1, weight=1)
        self.emergency_btn = ttk.Button(btns, text="EMERGENCY BACKUP", style="Secondary.TButton", command=lambda: self.start_backup(emergency=True))
        self.emergency_btn.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.backup_btn = ttk.Button(btns, text="BACK UP & COMPLETE", style="Primary.TButton", command=lambda: self.start_backup(emergency=False))
        self.backup_btn.grid(row=0, column=1, sticky="ew", padx=(5, 0))
        self.emergency_btn.state(["disabled"])

        for var in (self.date_var, self.initials_var, self.event_var, self.time_var):
            var.trace_add("write", lambda *_: self.update_folder_preview())
        self.update_folder_preview()
        self.update_backup_mode_labels()

    def is_autocorrect_mode(self) -> bool:
        return str(self.storage.get_setting("backup1_mode", "original") or "original").lower() == "auto_correct"

    def update_backup_mode_labels(self):
        if not hasattr(self, "backup_name_labels"):
            return
        if self.is_autocorrect_mode():
            self.backup_name_labels[1].configure(text="Backup 1 — working Auto Correct")
            self.backup_name_labels[2].configure(text="Backup 2 — original archive + temp safety")
            self.backup_name_labels[3].configure(text="Dropbox — original cloud archive")
        else:
            self.backup_name_labels[1].configure(text="Backup 1 — exact originals")
            self.backup_name_labels[2].configure(text="Backup 2 — exact originals")
            self.backup_name_labels[3].configure(text="Dropbox cloud")

    def load_reference_data(self):
        photographers = self.storage.get_setting("photographers", [])
        events = self.storage.get_setting("events", [])
        self.photographer_combo["values"] = [p["name"] for p in photographers]
        self.event_combo["values"] = [e["name"] for e in events]
        if photographers and not self.photographer_var.get():
            self.photographer_var.set(photographers[0]["name"])
            self.on_photographer_change()
        if events and not self.event_var.get():
            self.event_var.set(events[0]["name"])
            self.on_event_change()

    def on_photographer_change(self):
        name = self.photographer_var.get()
        for p in self.storage.get_setting("photographers", []):
            if p["name"] == name:
                self.initials_var.set(p["initials"])
                break

    def on_event_change(self):
        name = self.event_var.get()
        for e in self.storage.get_setting("events", []):
            if e["name"] == name:
                self.time_combo["values"] = e.get("times", [])
                if e.get("times"):
                    self.time_var.set(e["times"][0])
                elif not self.time_var.get():
                    self.time_var.set("")
                if hasattr(self, "quantity_label"):
                    self.quantity_label.configure(text="Number of bikes" if e.get("code", "").upper() == "ATV" else "Number of guests")
                break

    def on_issue_change(self):
        has_issue = self.issue_var.get().strip() != "No issues"
        if hasattr(self, "notes_label"):
            self.notes_label.configure(
                text="Issue details (required)" if has_issue else "Notes (optional)",
                fg=RED if has_issue else MUTED,
            )
        if has_issue and hasattr(self, "notes_entry"):
            self.notes_entry.focus_set()

    def update_folder_preview(self):
        try:
            event_code = self.current_event().get("code", "EVENT")
            folder = f"{date_code(self.date_var.get())}-{event_code}-{safe_time_code(self.time_var.get(), event_code)}-{self.initials_var.get().upper()}"
            self.folder_preview.configure(text=f"Folder preview: {folder}\nExample: {folder}-4821.JPG")
        except Exception:
            self.folder_preview.configure(text="Folder preview: —")

    def current_event(self) -> dict:
        name = self.event_var.get()
        for e in self.storage.get_setting("events", []):
            if e["name"] == name:
                return e
        return {"name": name, "code": "OTHER", "times": [], "guests_required": False}

    def monitor_sd_cards(self, initial: bool = False):
        """Watch for genuine camera-card candidates without ever considering network/fixed drives."""
        try:
            candidates = windows_camera_sd_candidates()
            current_ids = set(candidates.keys())

            if initial:
                new_ids = current_ids
            else:
                new_ids = current_ids - self.known_sd_cards

            self.known_sd_cards = current_ids

            if not self.backup_in_progress and new_ids:
                roots = [candidates[i] for i in new_ids if i in candidates]
                if len(roots) == 1:
                    root = roots[0]
                    self.source_var.set(root)
                    self.scan_source()
                    if getattr(self, "photos", None):
                        self.set_banner(f"SD card detected automatically on {root} — {len(self.photos)} JPEGs found.", GREEN)
                elif len(roots) > 1:
                    self.set_banner("Multiple new camera SD cards detected — choose the source card manually.", ORANGE)
        except Exception:
            # Auto-detection must never interfere with manual import or backups.
            pass
        finally:
            self.after(1200, self.monitor_sd_cards)

    def browse_source(self):
        p = filedialog.askdirectory(title="Select SD card or source folder", parent=self)
        if p:
            self.source_var.set(p)
            self.scan_source()

    def scan_source(self):
        src = self.source_var.get().strip()
        if not src:
            return
        self.set_banner("Scanning source for JPEG files…", BLUE)
        try:
            photos = discover_photos(src)
            self.photos: list[SourcePhoto] = photos
            self.thumbnail_generation += 1
            self.thumbnail_images.clear()
            self.rebuild_photo_tree()
            self.start_thumbnail_generation()
            if photos:
                self.set_banner(f"{len(photos)} JPEGs found. Review selections before backup.", GREEN)
            else:
                self.set_banner("No JPEG files found in this source.", ORANGE)
        except Exception as exc:
            messagebox.showerror("Scan failed", str(exc), parent=self)
            self.set_banner("Could not scan the selected source.", RED)

    def rebuild_photo_tree(self):
        self.photo_tree.delete(*self.photo_tree.get_children())
        for i, photo in enumerate(getattr(self, "photos", [])):
            thumb = self.thumbnail_images.get(photo.path, "")
            self.photo_tree.insert(
                "",
                "end",
                iid=str(i),
                image=thumb,
                text="",
                values=("✓" if photo.selected else "—", photo.original_name, photo.number or "?", human_bytes(photo.size)),
            )
        self.update_photo_summary()

    def start_thumbnail_generation(self):
        if not Image or not ImageTk:
            return
        generation = self.thumbnail_generation
        photos = list(getattr(self, "photos", []))
        missing = [(i, p.path) for i, p in enumerate(photos) if p.path not in self.thumbnail_images]
        if not missing:
            return

        def worker():
            for idx, path in missing:
                if generation != self.thumbnail_generation:
                    return
                try:
                    with Image.open(path) as source:
                        try:
                            source.draft("RGB", (112, 112))
                        except Exception:
                            pass
                        oriented = ImageOps.exif_transpose(source) if ImageOps else source.copy()
                        thumb = oriented.convert("RGB")
                        thumb.thumbnail((54, 44), Image.Resampling.LANCZOS)
                    tile = Image.new("RGB", (56, 46), "#1C252A")
                    x = (56 - thumb.width) // 2
                    y = (46 - thumb.height) // 2
                    tile.paste(thumb, (x, y))
                    self.msg_queue.put(("thumbnail_ready", (generation, idx, path, tile)))
                except Exception:
                    self.msg_queue.put(("thumbnail_ready", (generation, idx, path, None)))

        threading.Thread(target=worker, daemon=True, name="TPSPhotoThumbnails").start()

    def apply_thumbnail(self, payload):
        generation, idx, path, pil_image = payload
        if generation != self.thumbnail_generation:
            return
        if not pil_image or not ImageTk:
            return
        try:
            tk_image = ImageTk.PhotoImage(pil_image)
            self.thumbnail_images[path] = tk_image
            iid = str(idx)
            if self.photo_tree.exists(iid):
                self.photo_tree.item(iid, image=tk_image)
        except Exception:
            pass

    def update_photo_summary(self):
        photos = getattr(self, "photos", [])
        selected = [p for p in photos if p.selected]
        b = sum(p.size for p in selected)
        self.photo_summary.configure(text=f"{len(selected)} selected • {len(photos)-len(selected)} excluded • {human_bytes(b)}")

    def toggle_row(self, event=None):
        item = self.photo_tree.identify_row(event.y) if event else ""
        if not item:
            return
        idx = int(item)
        self.photos[idx].selected = not self.photos[idx].selected
        self.photo_tree.item(item, values=("✓" if self.photos[idx].selected else "—", self.photos[idx].original_name, self.photos[idx].number or "?", human_bytes(self.photos[idx].size)))
        self.update_photo_summary()

    def set_all_selected(self, selected: bool):
        for p in getattr(self, "photos", []):
            p.selected = selected
        self.rebuild_photo_tree()

    def invert_selection(self):
        for p in getattr(self, "photos", []):
            p.selected = not p.selected
        self.rebuild_photo_tree()

    def exclude_tree_selection(self):
        for iid in self.photo_tree.selection():
            self.photos[int(iid)].selected = False
        self.rebuild_photo_tree()

    def select_range(self):
        start = self.range_start.get().strip()
        end = self.range_end.get().strip()
        if not start or not end:
            return
        try:
            a, b = int(start), int(end)
        except ValueError:
            messagebox.showwarning("Invalid range", "Enter numeric camera file numbers.", parent=self)
            return
        if a > b:
            a, b = b, a
        for p in getattr(self, "photos", []):
            try:
                p.selected = a <= int(p.number) <= b
            except Exception:
                p.selected = False
        self.rebuild_photo_tree()

    def preview_selected(self, event=None):
        selected = self.photo_tree.selection()
        if not selected:
            return
        p = self.photos[int(selected[0])]
        self.preview_photo_path = p.path
        self.preview_name.configure(text=f"{p.original_name}  •  {human_bytes(p.size)}  •  {'Included' if p.selected else 'Excluded'}")
        self._render_preview()

    def _on_preview_resize(self, event=None):
        if self.preview_resize_after:
            try:
                self.after_cancel(self.preview_resize_after)
            except Exception:
                pass
        self.preview_resize_after = self.after(80, self._render_preview)

    def _render_preview(self):
        if not hasattr(self, "preview_canvas"):
            return
        canvas = self.preview_canvas
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())

        if not self.preview_photo_path:
            canvas.create_text(width // 2, height // 2, text="Select a photo to preview", fill="#9AA7AC", font=("Segoe UI", 10))
            return
        if not Image or not ImageTk:
            canvas.create_text(width // 2, height // 2, text="Preview requires Pillow", fill="#9AA7AC", font=("Segoe UI", 10))
            return

        try:
            with Image.open(self.preview_photo_path) as source:
                oriented = ImageOps.exif_transpose(source) if ImageOps else source.copy()
                im = oriented.copy()
            max_width = max(32, width - 8)
            max_height = max(32, height - 8)
            im.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
            self.photo_image = ImageTk.PhotoImage(im)
            canvas.create_image(width // 2, height // 2, image=self.photo_image, anchor="center")
        except Exception:
            canvas.create_text(width // 2, height // 2, text="Preview unavailable", fill="#9AA7AC", font=("Segoe UI", 10))

    def selected_bytes(self) -> int:
        return sum(p.size for p in getattr(self, "photos", []) if p.selected)

    def check_network(self):
        total = max(self.selected_bytes(), 1)
        self.update_backup_mode_labels()
        auto_mode = self.is_autocorrect_mode()
        self.set_banner("Checking required backup locations…", BLUE)
        checks = self.engine.preflight_all(total)

        b1_ok, b1_msg, b1_free = checks.get("backup1", (False, "Not available", 0))
        b2_ok, b2_msg, b2_free = checks.get("backup2", (False, "Not available", 0))
        all_ok = bool(b1_ok and b2_ok)

        self.backup_status_labels[1].configure(
            text=(("Ready for Auto Correct" if auto_mode else f"Ready • {human_bytes(b1_free)} free") if b1_ok else b1_msg),
            fg=(GREEN if b1_ok else RED),
        )

        if auto_mode:
            staging_ok, staging_msg, staging_free = checks.get("staging", (False, "Temporary safety not available", 0))
            all_ok = all_ok and bool(staging_ok)
            if b2_ok and staging_ok:
                b2_text = f"Original + temp safety ready • {human_bytes(min(b2_free, staging_free))} free"
                b2_color = GREEN
            else:
                b2_text = b2_msg if not b2_ok else staging_msg
                b2_color = RED
            self.backup_status_labels[2].configure(text=b2_text, fg=b2_color)
        else:
            self.backup_status_labels[2].configure(
                text=(f"Ready • {human_bytes(b2_free)} free" if b2_ok else b2_msg),
                fg=(GREEN if b2_ok else RED),
            )

        cloud_text = self.dropbox.connection_label()
        cloud_suffix = " • original upload after local safety" if auto_mode else " • uploads after 2/2 local verification"
        self.backup_status_labels[3].configure(
            text=cloud_text + cloud_suffix,
            fg=(GREEN if self.dropbox.configured() else ORANGE),
        )
        self.emergency_btn.state(["disabled"] if all_ok else ["!disabled"])
        if all_ok:
            if auto_mode:
                self.set_banner("Ready ✓ — Backup 2 + temporary originals protect the SD card; Backup 1 will be Auto Corrected.", GREEN)
            else:
                self.set_banner("Both local backup locations are connected and writable. Dropbox will not delay card release.", GREEN)
        else:
            self.set_banner("Required backup location unavailable — SD files are protected. Emergency local backup is available.", RED)
        return all_ok

    def validate_event_form(self) -> str | None:
        if not getattr(self, "photos", None):
            return "Select and scan an SD card/source first."
        if not any(p.selected for p in self.photos):
            return "Select at least one JPEG to import."
        if not self.photographer_var.get() or not self.initials_var.get():
            return "Choose a photographer."
        if not self.event_var.get():
            return "Choose an event."
        if not self.time_var.get().strip():
            return "Enter an event time."
        ev = self.current_event()
        if ev.get("guests_required") and not self.guests_var.get().strip():
            return "Number of bikes is required for ATV Quad Bikes." if ev.get("code", "").upper() == "ATV" else "Number of guests is required for this event."
        if self.guests_var.get().strip():
            try:
                if int(self.guests_var.get()) < 0:
                    raise ValueError
            except ValueError:
                return "Number of bikes must be a whole number." if ev.get("code", "").upper() == "ATV" else "Number of guests must be a whole number."
        if self.issue_var.get().strip() != "No issues" and not self.notes_var.get().strip():
            return f"Please enter details for the selected issue: {self.issue_var.get().strip()}."
        return None

    def build_spec(self) -> BackupJobSpec:
        ev = self.current_event()
        guests = int(self.guests_var.get()) if self.guests_var.get().strip() else None
        return BackupJobSpec(
            date_text=self.date_var.get().strip(), photographer=self.photographer_var.get().strip(),
            initials=self.initials_var.get().strip(), event_name=ev["name"], event_code=ev["code"],
            event_time=self.time_var.get().strip(), guests=guests, issue_type=self.issue_var.get().strip(), notes=self.notes_var.get().strip(),
            source_path=self.source_var.get().strip(), photos=self.photos,
            delete_after_verified=(self.delete_var.get() and False),  # deletion confirmation happens after 2/2 local completion in UI
        )

    def confirm_backup_details(self) -> bool:
        """Final human check of all manually entered event details before copying starts."""
        ev = self.current_event()
        selected = [p for p in getattr(self, "photos", []) if p.selected]
        excluded = len(getattr(self, "photos", [])) - len(selected)
        is_atv = str(ev.get("code", "")).upper() == "ATV"
        quantity_label = "Number of bikes" if is_atv else "Number of guests"
        quantity_value = self.guests_var.get().strip() or "Not entered"
        notes = self.notes_var.get().strip() or "None"
        issue = self.issue_var.get().strip() or "No issues"

        summary = (
            "Please confirm these event details before the backup starts:\n\n"
            f"Date: {self.date_var.get().strip()}\n"
            f"Photographer: {self.photographer_var.get().strip()} ({self.initials_var.get().strip()})\n"
            f"Event: {self.event_var.get().strip()}\n"
            f"Event time: {self.time_var.get().strip()}\n"
            f"{quantity_label}: {quantity_value}\n"
            f"Issue: {issue}\n"
            f"Issue / notes: {notes}\n\n"
            f"Photos selected: {len(selected)}\n"
            f"Photos excluded: {excluded}\n"
            f"Delete imported SD files after verification: {'Yes — final confirmation will still be required' if self.delete_var.get() else 'No'}\n\n"
            "Are these details correct and ready to back up?"
        )
        return messagebox.askyesno("Confirm event details", summary, parent=self, icon="question")

    def auto_check_connections_on_load(self):
        """Check active local safety paths, Dropbox and Event Log on startup without freezing the UI."""
        if self.backup_in_progress:
            return
        self.update_backup_mode_labels()

        if hasattr(self, "backup_status_labels"):
            self.backup_status_labels[1].configure(text="Checking…", fg=BLUE)
            self.backup_status_labels[2].configure(text="Checking…", fg=BLUE)
            self.backup_status_labels[3].configure(text="Checking Dropbox…", fg=BLUE)
        if hasattr(self, "eventlog_admin_status"):
            self.eventlog_admin_status.configure(text="Checking…", fg=BLUE)

        def worker():
            try:
                checks = self.engine.preflight_all(max(self.selected_bytes(), 1))
            except Exception as exc:
                checks = {
                    "backup1": (False, str(exc), 0),
                    "backup2": (False, str(exc), 0),
                    "staging": (False, str(exc), 0),
                }

            if self.dropbox.configured():
                try:
                    dropbox_ok, dropbox_msg = self.dropbox.test_connection()
                except Exception as exc:
                    dropbox_ok, dropbox_msg = False, str(exc)
            else:
                dropbox_ok, dropbox_msg = False, "Not connected"

            try:
                eventlog_ok, eventlog_msg = self.engine.eventlog.test_connection()
            except Exception as exc:
                eventlog_ok, eventlog_msg = False, str(exc)

            self.msg_queue.put((
                "startup_connections",
                (checks, dropbox_ok, dropbox_msg, eventlog_ok, eventlog_msg),
            ))

        threading.Thread(target=worker, daemon=True, name="TPSStartupConnectionCheck").start()

    def apply_startup_connection_results(self, payload):
        checks, dropbox_ok, dropbox_msg, eventlog_ok, eventlog_msg = payload
        auto_mode = self.is_autocorrect_mode()
        self.update_backup_mode_labels()

        b1_ok, b1_msg, b1_free = checks.get("backup1", (False, "Not available", 0))
        b2_ok, b2_msg, b2_free = checks.get("backup2", (False, "Not available", 0))
        local_ok = bool(b1_ok and b2_ok)

        if hasattr(self, "backup_status_labels"):
            self.backup_status_labels[1].configure(
                text=(("Ready for Auto Correct" if auto_mode else f"Ready • {human_bytes(b1_free)} free") if b1_ok else b1_msg),
                fg=(GREEN if b1_ok else RED),
            )

            if auto_mode:
                staging_ok, staging_msg, staging_free = checks.get("staging", (False, "Temporary safety not available", 0))
                local_ok = local_ok and bool(staging_ok)
                self.backup_status_labels[2].configure(
                    text=(f"Original + temp safety ready • {human_bytes(min(b2_free, staging_free))} free" if b2_ok and staging_ok else (b2_msg if not b2_ok else staging_msg)),
                    fg=(GREEN if b2_ok and staging_ok else RED),
                )
            else:
                self.backup_status_labels[2].configure(
                    text=(f"Ready • {human_bytes(b2_free)} free" if b2_ok else b2_msg),
                    fg=(GREEN if b2_ok else RED),
                )

            if dropbox_ok:
                self.backup_status_labels[3].configure(
                    text=("Connected ✓ • originals upload after local safety" if auto_mode else "Connected ✓ • uploads after 2/2 local verification"),
                    fg=GREEN,
                )
            elif self.dropbox.configured():
                self.backup_status_labels[3].configure(text=f"Dropbox issue • {dropbox_msg}", fg=ORANGE)
            else:
                self.backup_status_labels[3].configure(text="Not connected • cloud backups remain pending", fg=ORANGE)

        if hasattr(self, "eventlog_admin_status"):
            self.eventlog_admin_status.configure(
                text=("Connected and authorised ✓" if eventlog_ok else f"Connection issue • {eventlog_msg}"),
                fg=(GREEN if eventlog_ok else RED),
            )

        if hasattr(self, "emergency_btn"):
            self.emergency_btn.state(["disabled"] if local_ok else ["!disabled"])

        if not self.source_var.get().strip():
            if local_ok:
                cloud_note = "Dropbox connected" if dropbox_ok else "Dropbox pending/not connected"
                event_note = "Event Log connected" if eventlog_ok else "Event Log will retry"
                mode_note = "Auto Correct working mode" if auto_mode else "Exact-original mode"
                self.set_banner(f"Connections checked ✓ — {mode_note} ready • {cloud_note} • {event_note}. Insert an SD card.", GREEN)
            else:
                self.set_banner("Connection check found a local backup problem — Emergency Local Backup is available.", RED)

    def start_backup(self, emergency: bool):
        err = self.validate_event_form()
        if err:
            messagebox.showwarning("Check event", err, parent=self)
            return
        if emergency:
            if not messagebox.askyesno("Emergency local backup", "Use the admin-configured emergency local backup?\n\nThe SD card will NOT be deleted and the job will remain Network Backup Pending.", parent=self):
                return
        else:
            if not self.confirm_backup_details():
                return
            if not self.check_network():
                messagebox.showerror("Network backup unavailable", "At least one required local/filesystem backup location is unavailable.\n\nUse Emergency Local Backup instead. The SD card will not be deleted.", parent=self)
                return

        spec = self.build_spec()
        self.set_busy(True)
        self.progress["value"] = 0
        self.progress_text.configure(text="Starting…")

        def progress_cb(ev: ProgressEvent):
            self.msg_queue.put(("progress", ev))

        def worker():
            windows_keep_awake(True)
            try:
                manifest = self.engine.run_emergency(spec, progress_cb) if emergency else self.engine.run_normal(spec, progress_cb)
                self.msg_queue.put(("done", (manifest, emergency)))
            except Exception as exc:
                self.msg_queue.put(("error", str(exc)))
            finally:
                windows_keep_awake(False)

        threading.Thread(target=worker, daemon=True).start()

    def set_busy(self, busy: bool):
        self.backup_in_progress = busy
        if busy:
            self.backup_btn.state(["disabled"])
            self.emergency_btn.state(["disabled"])
        else:
            self.backup_btn.state(["!disabled"])
            self.check_network()

    def poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "thumbnail_ready":
                    self.apply_thumbnail(payload)
                elif kind == "startup_connections":
                    self.apply_startup_connection_results(payload)
                elif kind == "progress":
                    self.handle_progress(payload)
                elif kind == "done":
                    self.handle_done(*payload)
                elif kind == "error":
                    self.set_busy(False)
                    self.set_banner("Backup interrupted — originals remain on the SD card.", RED)
                    messagebox.showerror("Backup stopped safely", f"{payload}\n\nNo original SD files were deleted.", parent=self)
                elif kind == "history_done":
                    self.set_banner(payload, GREEN)
                    self.refresh_history()
                    self.start_pending_dropbox_sync()
                elif kind == "history_error":
                    messagebox.showerror("Could not complete backup", payload, parent=self)
                elif kind == "autocorrect_progress":
                    job_id, ev = payload
                    if hasattr(self, "backup_status_labels") and self.source_var.get().strip():
                        self.backup_status_labels[1].configure(text=f"Auto Correct • {ev.current}/{ev.total}", fg=BLUE)
                    self.refresh_history()
                elif kind == "autocorrect_done":
                    job_id = payload
                    if hasattr(self, "backup_status_labels") and self.source_var.get().strip():
                        self.backup_status_labels[1].configure(text="Auto Correct verified ✓", fg=GREEN)
                    self.refresh_history()
                elif kind == "autocorrect_error":
                    job_id, err = payload
                    if hasattr(self, "backup_status_labels") and self.source_var.get().strip():
                        self.backup_status_labels[1].configure(text="Auto Correct failed • originals safe", fg=RED)
                    self.refresh_history()
                    if self.current_page == "history":
                        messagebox.showwarning("Auto Correct", f"Auto Correct could not complete. Untouched originals remain safe in Backup 2 / Dropbox workflow.\n\n{err}", parent=self)
                elif kind == "restore_backup1_done":
                    job_id, archive_path = payload
                    self.refresh_history()
                    messagebox.showinfo(
                        "Backup 1 restored",
                        "Backup 1 has been rebuilt from the untouched Backup 2 originals.\n\n"
                        + (f"The previous Auto-Corrected folder was preserved at:\n{archive_path}" if archive_path else "No previous corrected folder needed to be moved."),
                        parent=self,
                    )
                elif kind == "restore_backup1_error":
                    messagebox.showerror("Could not restore Backup 1", str(payload), parent=self)
                elif kind == "dropbox_progress":
                    job_id, done, total, name = payload
                    if hasattr(self, "backup_status_labels") and self.source_var.get().strip():
                        self.backup_status_labels[3].configure(text=f"Uploading • {done}/{total} • {name}", fg=BLUE)
                    self.refresh_history()
                elif kind == "dropbox_done":
                    job_id = payload
                    if hasattr(self, "backup_status_labels") and self.source_var.get().strip():
                        self.backup_status_labels[3].configure(text="Cloud originals verified ✓", fg=GREEN)
                    self.refresh_history()
                    manifest = self.storage.load_manifest(job_id) or {}
                    if manifest.get("backup_mode") == "auto_correct":
                        if manifest.get("backups", {}).get("backup1", {}).get("status") == "autocorrect_verified":
                            self.set_banner("BACKUP 1 AUTO-CORRECTED + BACKUP 2 ORIGINALS + DROPBOX ORIGINALS VERIFIED ✓", GREEN)
                    else:
                        self.set_banner("2/2 LOCAL VERIFIED + DROPBOX VERIFIED ✓", GREEN)
                elif kind == "dropbox_error":
                    job_id, err = payload
                    if hasattr(self, "backup_status_labels") and self.source_var.get().strip():
                        self.backup_status_labels[3].configure(text="Pending • will retry later", fg=ORANGE)
                    self.refresh_history()
                elif kind == "eventlog_test":
                    ok, msg = payload
                    if hasattr(self, "eventlog_admin_status"):
                        self.eventlog_admin_status.configure(text=msg, fg=(GREEN if ok else RED))
                    if not ok and self.current_page == "admin":
                        messagebox.showerror("Event Log connection", msg, parent=self)
                elif kind == "eventlog_manual_sync_done":
                    results = payload
                    ok_count = sum(1 for _, success, _ in results if success)
                    failed = len(results) - ok_count
                    self.refresh_history()
                    messagebox.showinfo("Event Log sync", f"{ok_count} record(s) sent successfully.\n{failed} remain queued.", parent=self)
                elif kind == "eventlog_auto_sync_done":
                    results = payload
                    if results:
                        self.refresh_history()
                        if any(success for _, success, _ in results):
                            self.set_banner("Event Log queue synced successfully.", GREEN)
                elif kind == "eventlog_sync_error":
                    self.refresh_history()
        except queue.Empty:
            pass
        self.after(250, self.poll_queue)

    def handle_progress(self, ev: ProgressEvent):
        if ev.bytes_total and ev.bytes_total > 1000:
            pct = max(0, min(100, ev.bytes_done * 100 / ev.bytes_total))
            self.progress["value"] = pct
            rate = f" • {human_bytes(int(ev.rate_bps))}/s" if ev.rate_bps > 0 else ""
            self.progress_text.configure(text=f"{ev.message} • {pct:.0f}%{rate}")
        elif ev.total:
            pct = max(0, min(100, ev.current * 100 / ev.total))
            self.progress["value"] = pct
            self.progress_text.configure(text=f"{ev.message} • {ev.current}/{ev.total}")
        else:
            self.progress_text.configure(text=ev.message)
        if ev.stage == "primary_ready":
            self.set_banner("PRIMARY BACKUP READY ✓ — Backup 2 is continuing. Do not clear the SD card yet.", GREEN)
        elif ev.stage == "backup2_ready":
            self.set_banner("BACKUP 2 ORIGINALS VERIFIED ✓ — creating temporary exact-original safety copy.", GREEN)
        elif ev.stage == "originals_safe":
            self.set_banner("ORIGINALS SAFE ✓ — SD card can be cleared; Backup 1 Auto Correct and Dropbox can continue.", GREEN)

    def reset_import_screen(self, banner_text: str = "Ready — insert an SD card."):
        """Clear only the per-event/import state; keep Admin, Dropbox and system configuration."""
        self.thumbnail_generation += 1
        self.thumbnail_images.clear()
        self.photos = []
        self.preview_photo_path = None
        self.photo_image = None

        if hasattr(self, "source_var"):
            self.source_var.set("")
        if hasattr(self, "photo_tree"):
            self.photo_tree.delete(*self.photo_tree.get_children())
        if hasattr(self, "photo_summary"):
            self.photo_summary.configure(text="No card selected")
        if hasattr(self, "preview_canvas"):
            self.preview_canvas.delete("all")
            w = max(1, self.preview_canvas.winfo_width())
            h = max(1, self.preview_canvas.winfo_height())
            self.preview_canvas.create_text(
                w // 2, h // 2,
                text="Insert an SD card to begin",
                fill="#9AA7AC",
                font=("Segoe UI", 10),
            )
        if hasattr(self, "preview_name"):
            self.preview_name.configure(text="")

        self.date_var.set(date.today().isoformat())
        self.photographer_var.set("")
        self.initials_var.set("")
        self.event_var.set("")
        self.time_var.set("")
        self.guests_var.set("")
        self.issue_var.set("No issues")
        self.notes_var.set("")
        self.on_issue_change()

        self.range_start.set("")
        self.range_end.set("")
        self.progress["value"] = 0
        self.progress_text.configure(text="")
        self.folder_preview.configure(text="Folder preview: —")
        for label in getattr(self, "backup_status_labels", {}).values():
            label.configure(text="Not checked", fg=MUTED)
        self.delete_var.set(bool(self.storage.get_setting("delete_after_verified_default", False)))
        self.set_banner(banner_text, GREEN)

    def handle_done(self, manifest: dict, emergency: bool):
        self.set_busy(False)
        if emergency:
            self.progress["value"] = 100
            self.set_banner("Emergency backup verified — the normal backup workflow is still pending. DO NOT clear the SD card.", ORANGE)
            messagebox.showinfo(
                "Emergency backup complete",
                f"{len(manifest['selected_files'])} photos were copied and verified to emergency storage.\n\n"
                "The normal backup workflow is still required.\nThe SD card has NOT been deleted.",
                parent=self,
            )
            return

        self.progress["value"] = 100
        selected_n = len(manifest["selected_files"])
        excluded_n = manifest["excluded_count"]
        auto_mode = manifest.get("backup_mode") == "auto_correct"

        if auto_mode:
            self.start_autocorrect_for_job(manifest["job_id"])
            if self.dropbox.configured():
                self.start_dropbox_for_job(manifest["job_id"])
                cloud_note = "Dropbox original upload has started in the background."
            else:
                cloud_note = "Dropbox is not connected; the temporary original safety copy will remain until Dropbox can verify."
            self.set_banner("ORIGINALS SAFE ✓ — Backup 2 + temporary originals verified. Auto Correct and Dropbox are continuing.", GREEN)
        else:
            if self.dropbox.configured():
                self.set_banner("2/2 LOCAL VERIFIED — safe to remove/clear the SD card. Dropbox is uploading in the background.", GREEN)
                self.start_dropbox_for_job(manifest["job_id"])
                cloud_note = "Dropbox upload has started in the background."
            else:
                self.set_banner("2/2 LOCAL VERIFIED — safe to remove/clear the SD card. Dropbox is not connected yet.", GREEN)
                cloud_note = "Dropbox is not connected; the cloud copy will remain pending until it is configured."

        if self.delete_var.get():
            if auto_mode:
                safety_text = (
                    f"All {selected_n} selected JPEG originals are byte-for-byte verified in:\n"
                    "• Backup 2 — untouched original archive\n"
                    "• Temporary safety copy — on a separate drive/share\n\n"
                    "Backup 1 Auto Correct and Dropbox originals can continue after the SD card is removed."
                )
            else:
                safety_text = f"All {selected_n} selected JPEGs are byte-for-byte verified in BOTH local backup locations."

            yes = messagebox.askyesno(
                "Safe to clear imported files",
                safety_text
                + f"\n\nDelete ONLY those {selected_n} imported JPEGs from the SD card now?\n"
                + f"{excluded_n} excluded file(s) will remain untouched.",
                parent=self,
            )
            if yes:
                try:
                    self.engine.delete_verified_sources(manifest)
                    manifest["sd_deleted"] = True
                    self.storage.save_manifest(manifest["job_id"], manifest)
                    row = self.storage.get_job(manifest["job_id"])
                    if row:
                        row["sd_deleted"] = 1
                        row["updated_at"] = __import__("datetime").datetime.now().isoformat()
                        self.storage.upsert_job(row)

                    source_root = manifest.get("source_path", self.source_var.get().strip())
                    ejected, eject_message = windows_eject_removable_drive(source_root)
                    background_note = (
                        "Backup 1 Auto Correct and Dropbox originals can continue independently in the background."
                        if auto_mode else
                        "Dropbox and Event Log can continue independently in the background."
                    )
                    if ejected:
                        self.reset_import_screen("SD CARD EJECTED ✓ — ready for the next card.")
                        messagebox.showinfo(
                            "Import complete",
                            f"{selected_n} imported JPEGs were deleted after original-safety verification.\n"
                            f"{excluded_n} excluded file(s) were left untouched.\n\n"
                            f"{eject_message}.\nThe Import Event screen has been reset and is ready for the next SD card.\n\n"
                            + background_note,
                            parent=self,
                        )
                    else:
                        self.reset_import_screen("IMPORT COMPLETE ✓ — remove the SD card manually when ready.")
                        messagebox.showwarning(
                            "Photos deleted — remove SD card manually",
                            f"{selected_n} imported JPEGs were deleted after original-safety verification.\n"
                            f"{excluded_n} excluded file(s) remain on the card.\n\n"
                            f"Windows could not automatically eject the card:\n{eject_message}\n\n"
                            "No further access to the SD card is required. Remove it manually, then insert the next card.\n"
                            "The Import Event screen has already been reset.\n\n"
                            + background_note,
                            parent=self,
                        )
                except Exception as exc:
                    messagebox.showerror(
                        "Could not clear SD card",
                        f"The verified original backups remain safe, but the source files were NOT fully deleted.\n\n{exc}",
                        parent=self,
                    )
                    return
        else:
            if auto_mode:
                messagebox.showinfo(
                    "Originals protected",
                    f"{selected_n} original photos are safely verified in Backup 2 plus the temporary safety copy.\n"
                    f"{excluded_n} excluded file(s) were not imported.\n\n"
                    "The SD card originals were retained.\n"
                    "Backup 1 Auto Correct is processing in the background.\n"
                    + cloud_note,
                    parent=self,
                )
            else:
                messagebox.showinfo(
                    "Local backup complete",
                    f"{selected_n} photos verified in 2/2 local locations.\n{excluded_n} excluded file(s) were not imported.\n\n"
                    "The SD card originals were retained.\n"
                    + cloud_note,
                    parent=self,
                )

    def set_banner(self, text: str, color: str):
        bg = "#E7EEE9" if color == GREEN else "#F4EBDD" if color == ORANGE else "#F3E3E3" if color == RED else "#E3ECF0"
        self.status_banner.configure(bg=bg, highlightbackground=bg)
        self.status_dot.configure(bg=bg, fg=color)
        self.status_text.configure(bg=bg, text=text)

    # ---------------- History ----------------
    def build_history_page(self):
        page = self.pages["history"]
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)

        head = tk.Frame(page, bg=BG)
        head.grid(row=0, column=0, sticky="ew", padx=34, pady=(25, 14))
        ttk.Label(head, text="Backup History", style="Title.TLabel").pack(anchor="w")
        tk.Label(head, text=f"Version {APP_VERSION}", bg=BG, fg="#7D9299", font=("Segoe UI Semibold", 8)).pack(anchor="w", pady=(1, 0))
        ttk.Label(
            head,
            text="Filter and sort verified local backups, Dropbox uploads, emergency jobs and Event Log sync status.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(3, 0))

        card = tk.Frame(page, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        card.grid(row=1, column=0, sticky="nsew", padx=34, pady=(0, 24))
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(2, weight=1)

        actions = tk.Frame(card, bg=CARD)
        actions.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 8))
        ttk.Button(actions, text="Refresh", style="Secondary.TButton", command=self.refresh_history).pack(side="left")
        ttk.Button(
            actions,
            text="Complete selected emergency backup",
            style="Primary.TButton",
            command=self.complete_selected_emergency,
        ).pack(side="left", padx=8)
        ttk.Button(actions, text="Sync Event Log queue", style="Secondary.TButton", command=self.sync_eventlog_queue).pack(side="left")

        filters = tk.Frame(card, bg="#F6F8F8", highlightbackground=LINE, highlightthickness=1)
        filters.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 10))
        filters.grid_columnconfigure(7, weight=1)

        tk.Label(filters, text="Event", bg="#F6F8F8", fg=MUTED, font=("Segoe UI", 8)).grid(row=0, column=0, sticky="w", padx=(12, 4), pady=(8, 2))
        self.history_event_filter = tk.StringVar(value="All events")
        self.history_event_combo = ttk.Combobox(filters, textvariable=self.history_event_filter, state="readonly", width=18)
        self.history_event_combo.grid(row=1, column=0, sticky="ew", padx=(12, 8), pady=(0, 9))
        self.history_event_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_history())

        tk.Label(filters, text="Photographer", bg="#F6F8F8", fg=MUTED, font=("Segoe UI", 8)).grid(row=0, column=1, sticky="w", padx=4, pady=(8, 2))
        self.history_photographer_filter = tk.StringVar(value="All photographers")
        self.history_photographer_combo = ttk.Combobox(filters, textvariable=self.history_photographer_filter, state="readonly", width=18)
        self.history_photographer_combo.grid(row=1, column=1, sticky="ew", padx=4, pady=(0, 9))
        self.history_photographer_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_history())

        tk.Label(filters, text="Backup status", bg="#F6F8F8", fg=MUTED, font=("Segoe UI", 8)).grid(row=0, column=2, sticky="w", padx=4, pady=(8, 2))
        self.history_status_filter = tk.StringVar(value="All statuses")
        self.history_status_combo = ttk.Combobox(
            filters,
            textvariable=self.history_status_filter,
            state="readonly",
            width=20,
            values=[
                "All statuses",
                "2/2 local verified",
                "Dropbox verified",
                "Dropbox pending",
                "Emergency backup",
                "Interrupted / failed",
            ],
        )
        self.history_status_combo.grid(row=1, column=2, sticky="ew", padx=4, pady=(0, 9))
        self.history_status_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_history())

        tk.Label(filters, text="Search", bg="#F6F8F8", fg=MUTED, font=("Segoe UI", 8)).grid(row=0, column=3, sticky="w", padx=4, pady=(8, 2))
        self.history_search_var = tk.StringVar()
        history_search = ttk.Entry(filters, textvariable=self.history_search_var, width=24)
        history_search.grid(row=1, column=3, sticky="ew", padx=4, pady=(0, 9))
        self.history_search_var.trace_add("write", lambda *_: self.refresh_history())

        ttk.Button(filters, text="Clear filters", style="Secondary.TButton", command=self.clear_history_filters).grid(
            row=1, column=4, sticky="w", padx=(8, 4), pady=(0, 9)
        )
        self.history_result_label = tk.Label(filters, text="", bg="#F6F8F8", fg=MUTED, font=("Segoe UI Semibold", 8))
        self.history_result_label.grid(row=1, column=7, sticky="e", padx=(8, 12), pady=(0, 9))

        cols = ("date", "event", "photographer", "photos", "status", "dropbox", "eventlog", "sd")
        self.history_tree = ttk.Treeview(card, columns=cols, show="headings")
        self.history_headings = {
            "date": ("Date / time", 150),
            "event": ("Event", 180),
            "photographer": ("Photographer", 150),
            "photos": ("Photos", 80),
            "status": ("Local backup", 190),
            "dropbox": ("Dropbox", 125),
            "eventlog": ("Event Log", 120),
            "sd": ("SD cleared", 90),
        }
        for c in cols:
            label, width = self.history_headings[c]
            self.history_tree.heading(c, text=label, command=lambda col=c: self.sort_history_by(col))
            self.history_tree.column(c, width=width, anchor="w")
        history_wrap = tk.Frame(card, bg=CARD)
        history_wrap.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 16))
        history_wrap.grid_rowconfigure(0, weight=1)
        history_wrap.grid_columnconfigure(0, weight=1)
        self.history_tree.grid(in_=history_wrap, row=0, column=0, sticky="nsew", padx=0, pady=0)
        history_scroll = ttk.Scrollbar(history_wrap, orient="vertical", command=self.history_tree.yview)
        history_scroll.grid(row=0, column=1, sticky="ns")
        self.history_tree.configure(yscrollcommand=history_scroll.set)

        self.refresh_history()

    def _history_status_matches(self, row: dict, selected: str) -> bool:
        if selected == "All statuses":
            return True
        local_ok = row.get("primary_status") == "verified" and row.get("backup2_status") == "verified"
        dropbox_ok = row.get("backup3_status") == "verified"
        status_text = str(row.get("status", "")).lower()
        if selected == "2/2 local verified":
            return local_ok
        if selected == "Dropbox verified":
            return dropbox_ok
        if selected == "Dropbox pending":
            return local_ok and not dropbox_ok
        if selected == "Emergency backup":
            return row.get("emergency_status") == "verified" or "emergency" in status_text
        if selected == "Interrupted / failed":
            return "interrupt" in status_text or "fail" in status_text or bool(row.get("error_message"))
        return True

    def clear_history_filters(self):
        if hasattr(self, "history_event_filter"):
            self.history_event_filter.set("All events")
            self.history_photographer_filter.set("All photographers")
            self.history_status_filter.set("All statuses")
            self.history_search_var.set("")
        self.refresh_history()

    def sort_history_by(self, column: str):
        if self.history_sort_column == column:
            self.history_sort_reverse = not self.history_sort_reverse
        else:
            self.history_sort_column = column
            self.history_sort_reverse = column == "date"
        self.refresh_history()

    def _history_sort_value(self, row: dict):
        col = self.history_sort_column
        if col == "date":
            return str(row.get("created_at", ""))
        if col == "event":
            return (str(row.get("event_name", "")).lower(), str(row.get("event_time", "")))
        if col == "photographer":
            return str(row.get("photographer", "")).lower()
        if col == "photos":
            return int(row.get("selected_count") or 0)
        if col == "status":
            return str(row.get("status", "")).lower()
        if col == "dropbox":
            return str(row.get("backup3_status", "")).lower()
        if col == "eventlog":
            return str(row.get("eventlog_status", "")).lower()
        if col == "sd":
            return int(row.get("sd_deleted") or 0)
        return str(row.get("created_at", ""))

    def refresh_history(self):
        if not hasattr(self, "history_tree"):
            return

        rows = self.storage.list_jobs()

        event_names = sorted({str(r.get("event_name", "")) for r in rows if r.get("event_name")})
        photographer_names = sorted({str(r.get("photographer", "")) for r in rows if r.get("photographer")})

        if hasattr(self, "history_event_combo"):
            self.history_event_combo["values"] = ["All events"] + event_names
            if self.history_event_filter.get() not in self.history_event_combo["values"]:
                self.history_event_filter.set("All events")
        if hasattr(self, "history_photographer_combo"):
            self.history_photographer_combo["values"] = ["All photographers"] + photographer_names
            if self.history_photographer_filter.get() not in self.history_photographer_combo["values"]:
                self.history_photographer_filter.set("All photographers")

        event_filter = self.history_event_filter.get() if hasattr(self, "history_event_filter") else "All events"
        photographer_filter = self.history_photographer_filter.get() if hasattr(self, "history_photographer_filter") else "All photographers"
        status_filter = self.history_status_filter.get() if hasattr(self, "history_status_filter") else "All statuses"
        search = self.history_search_var.get().strip().lower() if hasattr(self, "history_search_var") else ""

        filtered = []
        for row in rows:
            if event_filter != "All events" and row.get("event_name") != event_filter:
                continue
            if photographer_filter != "All photographers" and row.get("photographer") != photographer_filter:
                continue
            if not self._history_status_matches(row, status_filter):
                continue
            if search:
                haystack = " ".join(
                    str(row.get(k, ""))
                    for k in ("created_at", "event_name", "event_code", "event_time", "photographer", "initials", "folder_name", "notes", "status")
                ).lower()
                if search not in haystack:
                    continue
            filtered.append(row)

        filtered.sort(key=self._history_sort_value, reverse=self.history_sort_reverse)

        self.history_tree.delete(*self.history_tree.get_children())
        for row in filtered:
            status = str(row["status"]).replace("_", " ").title()
            dropbox = str(row["backup3_status"]).replace("_", " ").title()
            self.history_tree.insert(
                "",
                "end",
                iid=row["id"],
                values=(
                    row["created_at"][:16].replace("T", " "),
                    f"{row['event_name']} {row['event_time']}",
                    row["photographer"],
                    row["selected_count"],
                    status,
                    dropbox,
                    row["eventlog_status"],
                    "Yes" if row["sd_deleted"] else "No",
                ),
            )

        if hasattr(self, "history_result_label"):
            self.history_result_label.configure(text=f"{len(filtered)} of {len(rows)} records")

        if hasattr(self, "history_headings"):
            for col, (label, _) in self.history_headings.items():
                arrow = ""
                if col == self.history_sort_column:
                    arrow = " ▼" if self.history_sort_reverse else " ▲"
                self.history_tree.heading(col, text=label + arrow, command=lambda c=col: self.sort_history_by(c))

    def complete_selected_emergency(self):
        sel = self.history_tree.selection()
        if not sel:
            messagebox.showwarning("Select a job", "Choose an emergency/pending backup first.", parent=self)
            return
        job_id = sel[0]
        row = self.storage.get_job(job_id)
        if not row or row["emergency_status"] != "verified":
            messagebox.showwarning("Not an emergency job", "The selected job does not have a verified emergency backup.", parent=self)
            return
        if not messagebox.askyesno("Complete network backup", "Copy this verified emergency backup to both configured normal backup locations now?", parent=self):
            return

        def cb(ev):
            self.msg_queue.put(("progress", ev))

        def worker():
            windows_keep_awake(True)
            try:
                self.engine.complete_emergency_to_network(job_id, cb)
                self.msg_queue.put(("history_done", "Emergency backup completed to both normal backup locations. Dropbox can now upload in the background."))
            except Exception as exc:
                self.msg_queue.put(("history_error", str(exc)))
            finally:
                windows_keep_awake(False)
        threading.Thread(target=worker, daemon=True).start()

    def sync_eventlog_queue(self):
        def worker():
            try:
                results = self.engine.eventlog.sync_all()
                self.msg_queue.put(("eventlog_manual_sync_done", results))
            except Exception as exc:
                self.msg_queue.put(("eventlog_sync_error", str(exc)))
        threading.Thread(target=worker, daemon=True, name="TPSEventLogManualSync").start()

    def auto_sync_eventlog_queue(self):
        def worker():
            try:
                results = self.engine.eventlog.sync_all()
                self.msg_queue.put(("eventlog_auto_sync_done", results))
            except Exception as exc:
                self.msg_queue.put(("eventlog_sync_error", str(exc)))
        threading.Thread(target=worker, daemon=True, name="TPSEventLogAutoSync").start()

    def admin_test_eventlog(self):
        if hasattr(self, "eventlog_admin_status"):
            self.eventlog_admin_status.configure(text="Testing…", fg=BLUE)

        def worker():
            try:
                ok, msg = self.engine.eventlog.test_connection()
                self.msg_queue.put(("eventlog_test", (ok, msg)))
            except Exception as exc:
                self.msg_queue.put(("eventlog_test", (False, str(exc))))
        threading.Thread(target=worker, daemon=True, name="TPSEventLogTest").start()

    def refresh_eventlog_admin_status(self):
        if hasattr(self, "eventlog_admin_status"):
            self.eventlog_admin_status.configure(text="Connected automatically", fg=GREEN)

    # ---------------- Admin ----------------
    def build_admin_page(self):
        page = self.pages["admin"]
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)
        head = tk.Frame(page, bg=BG)
        head.grid(row=0, column=0, sticky="ew", padx=34, pady=(25, 14))
        ttk.Label(head, text="Admin", style="Title.TLabel").pack(anchor="w")
        tk.Label(head, text=f"Version {APP_VERSION}", bg=BG, fg="#7D9299", font=("Segoe UI Semibold", 8)).pack(anchor="w", pady=(1, 0))
        ttk.Label(head, text="Locked local backup destinations, Dropbox cloud backup, emergency storage and system configuration.", style="Subtitle.TLabel").pack(anchor="w", pady=(3, 0))

        canvas = tk.Canvas(page, bg=BG, highlightthickness=0)
        self.admin_canvas = canvas
        canvas.grid(row=1, column=0, sticky="nsew", padx=34, pady=(0, 24))
        sb = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
        sb.grid(row=1, column=1, sticky="ns", pady=(0,24))
        canvas.configure(yscrollcommand=sb.set)
        inner = tk.Frame(canvas, bg=BG)
        self.admin_inner = inner
        win = canvas.create_window((0,0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        inner.grid_columnconfigure(0, weight=1)
        self.admin_inner = inner

        paths = tk.Frame(inner, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        paths.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        paths.grid_columnconfigure(1, weight=1)
        tk.Label(paths, text="BACKUP LOCATIONS", bg=CARD, fg=GOLD, font=("Segoe UI Semibold", 9)).grid(row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(14,8))
        self.admin_path_vars = {}
        for r, (key, label) in enumerate([
            ("backup1_path", "Backup 1 — PRIMARY / priority"),
            ("backup2_path", "Backup 2 — independent local/filesystem copy"),
            ("emergency_path", "Emergency local backup"),
        ], start=1):
            tk.Label(paths, text=label, bg=CARD, fg=INK, font=("Segoe UI Semibold", 9)).grid(row=r, column=0, sticky="w", padx=16, pady=7)
            var = tk.StringVar()
            self.admin_path_vars[key] = var
            ttk.Entry(paths, textvariable=var).grid(row=r, column=1, sticky="ew", padx=8, pady=7)
            ttk.Button(paths, text="Browse", style="Secondary.TButton", command=lambda v=var: self.admin_browse(v)).grid(row=r, column=2, padx=(0,16), pady=7)

        cloud = tk.Frame(inner, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        cloud.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        cloud.grid_columnconfigure(1, weight=1)
        tk.Label(cloud, text="DROPBOX CLOUD BACKUP", bg=CARD, fg=GOLD, font=("Segoe UI Semibold", 9)).grid(row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(14,8))
        tk.Label(cloud, text="Destination", bg=CARD, fg=INK, font=("Segoe UI Semibold", 9)).grid(row=1, column=0, sticky="w", padx=16, pady=7)
        tk.Label(cloud, text=DROPBOX_DESTINATION, bg=CARD, fg=INK, font=("Segoe UI", 9)).grid(row=1, column=1, columnspan=2, sticky="w", padx=(8,16), pady=7)
        tk.Label(cloud, text="Fixed in this app — staff cannot change the cloud destination.", bg=CARD, fg=MUTED, font=("Segoe UI", 8)).grid(row=2, column=1, columnspan=2, sticky="w", padx=(8,16), pady=(0,7))
        tk.Label(cloud, text="Dropbox App key", bg=CARD, fg=INK, font=("Segoe UI Semibold", 9)).grid(row=3, column=0, sticky="w", padx=16, pady=7)
        self.dropbox_app_key_var = tk.StringVar()
        ttk.Entry(cloud, textvariable=self.dropbox_app_key_var).grid(row=3, column=1, columnspan=2, sticky="ew", padx=(8,16), pady=7)
        tk.Label(cloud, text="One-time setup: create a Full Dropbox API app with account_info.read, files.metadata.read and files.content.write permissions.", bg=CARD, fg=MUTED, font=("Segoe UI", 8), wraplength=760, justify="left").grid(row=4, column=1, columnspan=2, sticky="w", padx=(8,16), pady=(0,7))
        tk.Label(cloud, text="Status", bg=CARD, fg=INK, font=("Segoe UI Semibold", 9)).grid(row=5, column=0, sticky="w", padx=16, pady=7)
        self.dropbox_admin_status = tk.Label(cloud, text="Not connected", bg=CARD, fg=ORANGE, font=("Segoe UI Semibold", 9))
        self.dropbox_admin_status.grid(row=5, column=1, sticky="w", padx=8, pady=7)
        dbbtns = tk.Frame(cloud, bg=CARD)
        dbbtns.grid(row=6, column=0, columnspan=3, sticky="w", padx=16, pady=(5,14))
        ttk.Button(dbbtns, text="CONNECT DROPBOX", style="Primary.TButton", command=self.admin_connect_dropbox).pack(side="left")
        ttk.Button(dbbtns, text="Test Dropbox", style="Secondary.TButton", command=self.admin_test_dropbox).pack(side="left", padx=8)
        ttk.Button(dbbtns, text="Disconnect", style="Secondary.TButton", command=self.admin_disconnect_dropbox).pack(side="left")

        syscard = tk.Frame(inner, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        syscard.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        syscard.grid_columnconfigure(1, weight=1)
        tk.Label(syscard, text="EVENT LOG & CARD CLEARING", bg=CARD, fg=GOLD, font=("Segoe UI Semibold", 9)).grid(row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(14,6))
        tk.Label(syscard, text="TPS Event Log integration", bg=CARD, fg=INK, font=("Segoe UI Semibold", 9)).grid(row=1, column=0, sticky="w", padx=16, pady=7)
        self.eventlog_admin_status = tk.Label(syscard, text="Checking…", bg=CARD, fg=MUTED, font=("Segoe UI Semibold", 9))
        self.eventlog_admin_status.grid(row=1, column=1, sticky="w", padx=8, pady=7)
        ttk.Button(syscard, text="Test Event Log", style="Secondary.TButton", command=self.admin_test_eventlog).grid(row=1, column=2, sticky="e", padx=(8,16), pady=7)
        tk.Label(
            syscard,
            text="Completed photo imports are sent automatically to the central Event Log import database. If the connection is unavailable, records stay queued locally and retry later.",
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI", 8),
            wraplength=900,
            justify="left",
        ).grid(row=2, column=0, columnspan=3, sticky="w", padx=16, pady=(0,10))
        self.admin_delete_default_var = tk.BooleanVar()
        ttk.Checkbutton(syscard, text="Pre-select 'Delete imported SD files after 2 local backups are verified'", variable=self.admin_delete_default_var).grid(row=3, column=0, columnspan=3, sticky="w", padx=16, pady=(7,4))
        tk.Label(syscard, text="Even when pre-selected, staff still receive a final confirmation. Dropbox and Event Log sync are not required before the SD card can be cleared.", bg=CARD, fg=MUTED, font=("Segoe UI", 8), wraplength=900, justify="left").grid(row=4, column=0, columnspan=3, sticky="w", padx=16, pady=(0,14))

        people = tk.Frame(inner, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        people.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        people.grid_columnconfigure(0, weight=1)
        tk.Label(people, text="PHOTOGRAPHERS", bg=CARD, fg=GOLD, font=("Segoe UI Semibold", 9)).grid(row=0, column=0, sticky="w", padx=16, pady=(14,8))
        self.people_tree = ttk.Treeview(people, columns=("name","initials"), show="headings", height=7)
        self.people_tree.heading("name", text="Name")
        self.people_tree.heading("initials", text="Initials")
        self.people_tree.column("name", width=260)
        self.people_tree.column("initials", width=100)
        self.people_tree.grid(row=1, column=0, sticky="ew", padx=16)
        pb = tk.Frame(people, bg=CARD)
        pb.grid(row=2, column=0, sticky="w", padx=16, pady=12)
        ttk.Button(pb, text="Add", style="Secondary.TButton", command=self.admin_add_person).pack(side="left")
        ttk.Button(pb, text="Edit", style="Secondary.TButton", command=self.admin_edit_person).pack(side="left", padx=6)
        ttk.Button(pb, text="Remove", style="Secondary.TButton", command=self.admin_remove_person).pack(side="left")

        events = tk.Frame(inner, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        events.grid(row=4, column=0, sticky="ew", pady=(0, 12))
        events.grid_columnconfigure(0, weight=1)
        tk.Label(events, text="EVENT TYPES", bg=CARD, fg=GOLD, font=("Segoe UI Semibold", 9)).grid(row=0, column=0, sticky="w", padx=16, pady=(14,8))
        self.events_tree = ttk.Treeview(events, columns=("name","code","times","guests"), show="headings", height=7)
        for c, text, w in [("name","Event",220),("code","Code",80),("times","Standard times",340),("guests","Guests required",120)]:
            self.events_tree.heading(c, text=text); self.events_tree.column(c, width=w)
        self.events_tree.grid(row=1, column=0, sticky="ew", padx=16)
        eb = tk.Frame(events, bg=CARD)
        eb.grid(row=2, column=0, sticky="w", padx=16, pady=12)
        ttk.Button(eb, text="Add", style="Secondary.TButton", command=self.admin_add_event).pack(side="left")
        ttk.Button(eb, text="Edit", style="Secondary.TButton", command=self.admin_edit_event).pack(side="left", padx=6)
        ttk.Button(eb, text="Remove", style="Secondary.TButton", command=self.admin_remove_event).pack(side="left")

        savebar = tk.Frame(inner, bg=BG)
        savebar.grid(row=5, column=0, sticky="ew", pady=(2, 20))
        ttk.Button(savebar, text="Test connections", style="Secondary.TButton", command=self.admin_test_locations).pack(side="left")
        ttk.Button(savebar, text="Run speed test", style="Secondary.TButton", command=self.admin_speed_test).pack(side="left", padx=8)
        ttk.Button(savebar, text="SAVE ADMIN SETTINGS", style="Primary.TButton", command=self.save_admin_settings).pack(side="right")

    def admin_browse(self, var: tk.StringVar):
        p = filedialog.askdirectory(title="Select folder", parent=self)
        if p:
            var.set(p)

    def load_admin_settings(self):
        for key, var in getattr(self, "admin_path_vars", {}).items():
            var.set(self.storage.get_setting(key, ""))
        if hasattr(self, "admin_delete_default_var"):
            self.admin_delete_default_var.set(bool(self.storage.get_setting("delete_after_verified_default", False)))
            self.dropbox_app_key_var.set(self.storage.get_setting("dropbox_app_key", ""))
            self.refresh_dropbox_admin_status()
            self.refresh_eventlog_admin_status()
            self.refresh_admin_people()
            self.refresh_admin_events()

    def refresh_admin_people(self):
        self.people_tree.delete(*self.people_tree.get_children())
        for i, p in enumerate(self.storage.get_setting("photographers", [])):
            self.people_tree.insert("", "end", iid=str(i), values=(p["name"], p["initials"]))

    def admin_add_person(self):
        name = simpledialog.askstring("Add photographer", "Name", parent=self)
        if not name: return
        initials = simpledialog.askstring("Add photographer", "Initials", parent=self)
        if not initials: return
        people = self.storage.get_setting("photographers", [])
        people.append({"name": name.strip(), "initials": initials.strip().upper()})
        self.storage.set_setting("photographers", people)
        self.refresh_admin_people()

    def admin_edit_person(self):
        sel = self.people_tree.selection()
        if not sel: return
        idx = int(sel[0]); people = self.storage.get_setting("photographers", [])
        p = people[idx]
        name = simpledialog.askstring("Edit photographer", "Name", initialvalue=p["name"], parent=self)
        if not name: return
        initials = simpledialog.askstring("Edit photographer", "Initials", initialvalue=p["initials"], parent=self)
        if not initials: return
        people[idx] = {"name": name.strip(), "initials": initials.strip().upper()}
        self.storage.set_setting("photographers", people); self.refresh_admin_people()

    def admin_remove_person(self):
        sel = self.people_tree.selection()
        if not sel: return
        people = self.storage.get_setting("photographers", [])
        del people[int(sel[0])]
        self.storage.set_setting("photographers", people); self.refresh_admin_people()

    def refresh_admin_events(self):
        self.events_tree.delete(*self.events_tree.get_children())
        for i, e in enumerate(self.storage.get_setting("events", [])):
            self.events_tree.insert("", "end", iid=str(i), values=(e["name"], e["code"], ", ".join(e.get("times", [])), "Yes" if e.get("guests_required") else "No"))

    def _event_dialog(self, existing=None):
        existing = existing or {"name":"", "code":"", "times":[], "guests_required":False}
        name = simpledialog.askstring("Event", "Event name", initialvalue=existing["name"], parent=self)
        if not name: return None
        code = simpledialog.askstring("Event", "Filename code (e.g. SAND)", initialvalue=existing["code"], parent=self)
        if not code: return None
        times = simpledialog.askstring("Event", "Standard times, comma separated (optional)", initialvalue=", ".join(existing.get("times", [])), parent=self) or ""
        guests = messagebox.askyesno("Number of guests", "Require Number of Guests for this event?", parent=self)
        return {"name": name.strip(), "code": code.strip().upper(), "times": [x.strip() for x in times.split(",") if x.strip()], "guests_required": guests}

    def admin_add_event(self):
        e = self._event_dialog()
        if not e: return
        events = self.storage.get_setting("events", []); events.append(e); self.storage.set_setting("events", events); self.refresh_admin_events()

    def admin_edit_event(self):
        sel = self.events_tree.selection()
        if not sel: return
        idx = int(sel[0]); events = self.storage.get_setting("events", [])
        e = self._event_dialog(events[idx])
        if not e: return
        events[idx] = e; self.storage.set_setting("events", events); self.refresh_admin_events()

    def admin_remove_event(self):
        sel = self.events_tree.selection()
        if not sel: return
        events = self.storage.get_setting("events", []); del events[int(sel[0])]; self.storage.set_setting("events", events); self.refresh_admin_events()

    def save_admin_settings(self, show_message: bool = True):
        for key, var in self.admin_path_vars.items():
            self.storage.set_setting(key, var.get().strip())
        self.storage.set_setting("dropbox_app_key", self.dropbox_app_key_var.get().strip())
        self.storage.set_setting("delete_after_verified_default", bool(self.admin_delete_default_var.get()))
        self.delete_var.set(bool(self.admin_delete_default_var.get()))
        self.load_reference_data()
        if show_message:
            messagebox.showinfo("Admin settings", "Settings saved. Staff cannot edit the configured backup locations from the Import screen.", parent=self)

    def admin_test_locations(self):
        self.save_admin_settings(show_message=False)
        checks = self.engine.preflight_all(1024 * 1024)
        lines = []
        for key in ("backup1","backup2"):
            ok, msg, free = checks[key]
            lines.append(f"{key.upper()}: {'READY' if ok else 'FAILED'} — {msg}" + (f" — {human_bytes(free)} free" if ok else ""))
        epath = self.storage.get_setting("emergency_path", "")
        ok, msg, free = self.engine.preflight_path(epath, 1024 * 1024)
        lines.append(f"EMERGENCY: {'READY' if ok else 'FAILED'} — {msg}" + (f" — {human_bytes(free)} free" if ok else ""))
        messagebox.showinfo("Backup location test", "\n".join(lines), parent=self)

    def admin_speed_test(self):
        self.save_admin_settings(show_message=False)
        if not messagebox.askyesno("Backup speed test", "Write and read a temporary 32 MB test file on each configured location?\n\nNo photo files are changed.", parent=self):
            return
        lines = []
        for label, key in [("Backup 1", "backup1_path"), ("Backup 2", "backup2_path"), ("Emergency", "emergency_path")]:
            raw = self.storage.get_setting(key, "")
            ok, msg, write_mbps, read_mbps = self.engine.benchmark_path(raw, 32)
            if ok:
                lines.append(f"{label}: write {write_mbps:.1f} MB/s • read {read_mbps:.1f} MB/s")
            else:
                lines.append(f"{label}: FAILED — {msg}")
        messagebox.showinfo("Backup speed test", "\n".join(lines), parent=self)

    def refresh_dropbox_admin_status(self):
        if not hasattr(self, "dropbox_admin_status"):
            return
        label = self.dropbox.connection_label()
        self.dropbox_admin_status.configure(text=label, fg=(GREEN if self.dropbox.configured() else ORANGE))

    def admin_connect_dropbox(self):
        app_key = self.dropbox_app_key_var.get().strip()
        if not app_key:
            messagebox.showwarning("Dropbox App key", "Enter the Dropbox App key first. The Dropbox API app must use Full Dropbox access.", parent=self)
            return
        self.storage.set_setting("dropbox_app_key", app_key)
        try:
            self.dropbox.start_oauth(app_key)
            code = simpledialog.askstring(
                "Connect Dropbox",
                "A Dropbox authorization page has opened in your browser.\n\nApprove the TPS Photo Import & Backup app, copy the authorization code shown by Dropbox, then paste it here:",
                parent=self,
            )
            if not code:
                return
            name, email = self.dropbox.finish_oauth(code, app_key)
            self.refresh_dropbox_admin_status()
            ok, msg = self.dropbox.test_connection()
            if ok:
                messagebox.showinfo("Dropbox connected", f"Connected as {name} ({email}).\n\nCloud backups will upload to:\n{DROPBOX_DESTINATION}", parent=self)
                self.start_pending_dropbox_sync()
            else:
                messagebox.showwarning("Dropbox connected but destination unavailable", f"The account connection succeeded, but the destination could not be accessed.\n\n{msg}\n\nConfirm the Dropbox app uses Full Dropbox access and your account can edit the TPS folder.", parent=self)
        except Exception as exc:
            messagebox.showerror("Dropbox connection failed", str(exc), parent=self)

    def admin_test_dropbox(self):
        self.storage.set_setting("dropbox_app_key", self.dropbox_app_key_var.get().strip())
        ok, msg = self.dropbox.test_connection()
        self.refresh_dropbox_admin_status()
        if ok:
            messagebox.showinfo("Dropbox test", f"{msg}\n\nDestination:\n{DROPBOX_DESTINATION}", parent=self)
        else:
            messagebox.showerror("Dropbox test failed", msg, parent=self)

    def admin_disconnect_dropbox(self):
        if not messagebox.askyesno("Disconnect Dropbox", "Disconnect this computer from Dropbox? Pending cloud backups will remain queued and local backups are unaffected.", parent=self):
            return
        self.dropbox.disconnect()
        self.refresh_dropbox_admin_status()
        if hasattr(self, "backup_status_labels"):
            self.backup_status_labels[3].configure(text="Not connected • cloud backups remain pending", fg=ORANGE)

    def start_autocorrect_for_job(self, job_id: str):
        if self.autocorrect_thread and self.autocorrect_thread.is_alive():
            return

        def progress(ev: ProgressEvent):
            self.msg_queue.put(("autocorrect_progress", (job_id, ev)))

        def worker():
            windows_keep_awake(True)
            try:
                self.engine.build_autocorrect_backup1(job_id, progress)
                self.msg_queue.put(("autocorrect_done", job_id))
            except Exception as exc:
                self.msg_queue.put(("autocorrect_error", (job_id, str(exc))))
            finally:
                windows_keep_awake(False)

        self.autocorrect_thread = threading.Thread(target=worker, daemon=True, name="TPSAutoCorrect")
        self.autocorrect_thread.start()

    def start_dropbox_for_job(self, job_id: str):
        if not self.dropbox.configured():
            return
        if self.dropbox_thread and self.dropbox_thread.is_alive():
            return

        def progress(done, total, name):
            self.msg_queue.put(("dropbox_progress", (job_id, done, total, name)))

        def worker():
            try:
                self.dropbox.upload_job(job_id, progress)
                self.msg_queue.put(("dropbox_done", job_id))
            except Exception as exc:
                self.dropbox.mark_error(job_id, str(exc))
                self.msg_queue.put(("dropbox_error", (job_id, str(exc))))

        self.dropbox_thread = threading.Thread(target=worker, daemon=True, name="TPSDropboxUpload")
        self.dropbox_thread.start()

    def start_pending_dropbox_sync(self):
        if not self.dropbox.configured():
            return
        if self.dropbox_thread and self.dropbox_thread.is_alive():
            return
        pending = self.dropbox.pending_jobs(limit=100)
        if not pending:
            return

        def progress(done, total, name):
            self.msg_queue.put(("dropbox_progress", (pending[0], done, total, name)))

        def worker():
            for job_id in pending:
                try:
                    self.dropbox.upload_job(job_id, progress)
                    self.msg_queue.put(("dropbox_done", job_id))
                except Exception as exc:
                    self.dropbox.mark_error(job_id, str(exc))
                    self.msg_queue.put(("dropbox_error", (job_id, str(exc))))
                    # Most failures are connectivity/auth related; wait for next launch/manual test.
                    break

        self.dropbox_thread = threading.Thread(target=worker, daemon=True, name="TPSDropboxPending")
        self.dropbox_thread.start()

    def on_close(self):
        cloud_running = bool(self.dropbox_thread and self.dropbox_thread.is_alive())
        autocorrect_running = bool(self.autocorrect_thread and self.autocorrect_thread.is_alive())
        msg = "Exit the application?"
        if cloud_running or autocorrect_running:
            active = []
            if autocorrect_running:
                active.append("Backup 1 Auto Correct")
            if cloud_running:
                active.append("Dropbox upload")
            msg = (
                f"{' and '.join(active)} is still in progress.\n\n"
                "Untouched originals remain safe in Backup 2 / the original-safety workflow. "
                "Dropbox jobs resume automatically next launch; Auto Correct can always be rebuilt from Backup 2.\n\n"
                "Exit now?"
            )
        if messagebox.askokcancel("Exit TPS Photo Import", msg, parent=self):
            self.destroy()
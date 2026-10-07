from __future__ import annotations

import ctypes
import hashlib
import os
import queue
import sys
import threading
from datetime import date
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

try:
    from PIL import Image, ImageTk
except Exception:  # pragma: no cover
    Image = None
    ImageTk = None

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
        self.msg_queue: queue.Queue = queue.Queue()
        self.current_page = "import"
        self.photo_image = None
        self.admin_unlocked = False
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        ico = resource_path("tps_photo_backup.ico")
        try:
            if ico.exists():
                self.iconbitmap(str(ico))
        except Exception:
            pass

        self._style()
        self._layout()
        self.after(300, self.poll_queue)
        self.after(700, self.auto_detect_source)
        self.after(1800, self.start_pending_dropbox_sync)

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
        tk.Label(self.nav, text="VERSION 1.1.2\nWindows desktop edition\n2 local verified • Dropbox cloud • Offline-safe", bg=NAV, fg="#8FA6AD", justify="left", font=("Segoe UI", 8)).pack(anchor="w", padx=22, pady=20)

        self.pages = {}
        for key in ("import", "history", "admin"):
            frame = tk.Frame(self.content, bg=BG)
            frame.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = frame
        self.build_import_page()
        self.build_history_page()
        self.build_admin_page()
        self.show_page("import")

    def show_page(self, key: str):
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
        tk.Label(event, text="Number of guests", bg=CARD, fg=MUTED, font=("Segoe UI", 8)).grid(row=3, column=2, sticky="w", padx=8)
        ttk.Entry(event, textvariable=self.guests_var, width=14).grid(row=4, column=2, columnspan=2, sticky="ew", padx=8, pady=(2, 14))
        tk.Label(event, text="Anything to report?", bg=CARD, fg=MUTED, font=("Segoe UI", 8)).grid(row=3, column=4, sticky="w", padx=8)
        ttk.Combobox(event, textvariable=self.issue_var, state="readonly", values=["No issues", "Photography issue", "Equipment issue", "Guest / operational issue", "Other"], width=20).grid(row=4, column=4, columnspan=2, sticky="ew", padx=8, pady=(2, 14))
        tk.Label(event, text="Notes / issue details (optional)", bg=CARD, fg=MUTED, font=("Segoe UI", 8)).grid(row=3, column=6, sticky="w", padx=8)
        ttk.Entry(event, textvariable=self.notes_var).grid(row=4, column=6, columnspan=2, sticky="ew", padx=(8, 18), pady=(2, 14))

        self.load_reference_data()

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
        self.photo_tree = ttk.Treeview(tree_wrap, columns=("include", "original", "number", "size"), show="headings", selectmode="extended")
        for col, text, width in [("include", "Import", 65), ("original", "Original filename", 200), ("number", "No.", 70), ("size", "Size", 80)]:
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
        right.grid_rowconfigure(0, weight=1)

        preview = tk.Frame(right, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        preview.grid(row=0, column=0, sticky="nsew", pady=(0, 12))
        preview.grid_columnconfigure(0, weight=1)
        preview.grid_rowconfigure(1, weight=1)
        tk.Label(preview, text="PREVIEW", bg=CARD, fg=GOLD, font=("Segoe UI Semibold", 9)).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 8))
        self.preview_label = tk.Label(preview, text="Select a photo to preview", bg="#EDE8DE", fg=MUTED, font=("Segoe UI", 10))
        self.preview_label.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 10))
        self.preview_name = tk.Label(preview, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 8))
        self.preview_name.grid(row=2, column=0, sticky="w", padx=16, pady=(0, 12))

        backup = tk.Frame(right, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        backup.grid(row=1, column=0, sticky="ew")
        backup.grid_columnconfigure(0, weight=1)
        tk.Label(backup, text="BACKUP READINESS", bg=CARD, fg=GOLD, font=("Segoe UI Semibold", 9)).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 8))
        self.backup_status_labels = {}
        for i, label in enumerate(("Primary backup", "Backup 2", "Dropbox cloud"), start=1):
            row = tk.Frame(backup, bg=CARD)
            row.grid(row=i, column=0, sticky="ew", padx=16, pady=2)
            tk.Label(row, text=label, bg=CARD, fg=INK, font=("Segoe UI Semibold", 9)).pack(side="left")
            status = tk.Label(row, text="Not checked", bg=CARD, fg=MUTED, font=("Segoe UI", 9))
            status.pack(side="right")
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
                break

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

    def auto_detect_source(self):
        drives = windows_removable_drives()
        if len(drives) == 1 and not self.source_var.get():
            self.source_var.set(drives[0])
            self.scan_source()
        elif len(drives) > 1 and not self.source_var.get():
            self.set_banner("Multiple removable drives detected — choose the SD card source.", ORANGE)

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
            self.rebuild_photo_tree()
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
            self.photo_tree.insert("", "end", iid=str(i), values=("✓" if photo.selected else "—", photo.original_name, photo.number or "?", human_bytes(photo.size)))
        self.update_photo_summary()

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
        self.preview_name.configure(text=f"{p.original_name}  •  {human_bytes(p.size)}  •  {'Included' if p.selected else 'Excluded'}")
        if not Image or not ImageTk:
            self.preview_label.configure(image="", text="Preview requires Pillow")
            return
        try:
            im = Image.open(p.path)
            im.thumbnail((480, 310))
            self.photo_image = ImageTk.PhotoImage(im.copy())
            self.preview_label.configure(image=self.photo_image, text="", bg="#1C252A")
        except Exception:
            self.preview_label.configure(image="", text="Preview unavailable", bg="#EDE8DE")

    def selected_bytes(self) -> int:
        return sum(p.size for p in getattr(self, "photos", []) if p.selected)

    def check_network(self):
        total = self.selected_bytes()
        if total <= 0:
            total = 1
        self.set_banner("Checking both required backup locations…", BLUE)
        checks = self.engine.preflight_all(total)
        all_ok = True
        for i, key in enumerate(("backup1", "backup2"), start=1):
            ok, msg, free = checks[key]
            all_ok &= ok
            self.backup_status_labels[i].configure(text=(f"Ready • {human_bytes(free)} free" if ok else msg), fg=(GREEN if ok else RED))
        cloud_text = self.dropbox.connection_label()
        self.backup_status_labels[3].configure(text=(cloud_text + " • uploads after 2/2 local verification"), fg=(GREEN if self.dropbox.configured() else ORANGE))
        self.emergency_btn.state(["disabled"] if all_ok else ["!disabled"])
        if all_ok:
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
            return "Number of guests is required for this event."
        if self.guests_var.get().strip():
            try:
                if int(self.guests_var.get()) < 0:
                    raise ValueError
            except ValueError:
                return "Number of guests must be a whole number."
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

    def start_backup(self, emergency: bool):
        err = self.validate_event_form()
        if err:
            messagebox.showwarning("Check event", err, parent=self)
            return
        if emergency:
            if not messagebox.askyesno("Emergency local backup", "Use the admin-configured emergency local backup?\n\nThe SD card will NOT be deleted and the job will remain Network Backup Pending.", parent=self):
                return
        else:
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
                if kind == "progress":
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
                elif kind == "dropbox_progress":
                    job_id, done, total, name = payload
                    if hasattr(self, "backup_status_labels"):
                        self.backup_status_labels[3].configure(text=f"Uploading • {done}/{total} • {name}", fg=BLUE)
                    self.refresh_history()
                elif kind == "dropbox_done":
                    if hasattr(self, "backup_status_labels"):
                        self.backup_status_labels[3].configure(text="Cloud backup verified ✓", fg=GREEN)
                    self.refresh_history()
                    self.set_banner("2/2 LOCAL VERIFIED + DROPBOX VERIFIED ✓", GREEN)
                elif kind == "dropbox_error":
                    job_id, err = payload
                    if hasattr(self, "backup_status_labels"):
                        self.backup_status_labels[3].configure(text="Pending • will retry later", fg=ORANGE)
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

    def handle_done(self, manifest: dict, emergency: bool):
        self.set_busy(False)
        if emergency:
            self.progress["value"] = 100
            self.set_banner("Emergency backup verified — the normal 2 local backups are still pending. DO NOT clear the SD card.", ORANGE)
            messagebox.showinfo("Emergency backup complete", f"{len(manifest['selected_files'])} photos were copied and verified to emergency storage.\n\nThe two normal local backups are still required.\nThe SD card has NOT been deleted.", parent=self)
            return

        self.progress["value"] = 100
        selected_n = len(manifest["selected_files"])
        excluded_n = manifest["excluded_count"]
        if self.dropbox.configured():
            self.set_banner("2/2 LOCAL VERIFIED — safe to remove/clear the SD card. Dropbox is uploading in the background.", GREEN)
            self.start_dropbox_for_job(manifest["job_id"])
        else:
            self.set_banner("2/2 LOCAL VERIFIED — safe to remove/clear the SD card. Dropbox is not connected yet.", GREEN)

        if self.delete_var.get():
            yes = messagebox.askyesno(
                "Safe to clear imported files",
                f"All {selected_n} selected JPEGs are byte-for-byte verified in BOTH local backup locations.\n\n"
                f"Delete ONLY those {selected_n} imported JPEGs from the SD card now?\n"
                f"{excluded_n} excluded file(s) will remain untouched.\n\n"
                "Dropbox continues independently from Backup 1 and does not require the SD card.",
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
                    messagebox.showinfo("SD card updated", f"{selected_n} imported JPEGs were deleted.\n{excluded_n} excluded file(s) remain on the card.\n\nDropbox can continue uploading from the verified primary backup.", parent=self)
                except Exception as exc:
                    messagebox.showerror("Could not clear SD card", f"The local backups remain safe, but the source files were NOT fully deleted.\n\n{exc}", parent=self)
                    return
        else:
            cloud_note = "Dropbox upload has started in the background." if self.dropbox.configured() else "Dropbox is not connected; the cloud copy will remain pending until it is configured."
            messagebox.showinfo("Local backup complete", f"{selected_n} photos verified in 2/2 local locations.\n{excluded_n} excluded file(s) were not imported.\n\nThe SD card originals were retained.\n{cloud_note}", parent=self)

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
        ttk.Label(head, text="Verified local backups, Dropbox upload status, emergency jobs and Event Log sync status.", style="Subtitle.TLabel").pack(anchor="w", pady=(3, 0))

        card = tk.Frame(page, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        card.grid(row=1, column=0, sticky="nsew", padx=34, pady=(0, 24))
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(1, weight=1)
        bar = tk.Frame(card, bg=CARD)
        bar.grid(row=0, column=0, sticky="ew", padx=16, pady=14)
        ttk.Button(bar, text="Refresh", style="Secondary.TButton", command=self.refresh_history).pack(side="left")
        ttk.Button(bar, text="Complete selected emergency backup", style="Primary.TButton", command=self.complete_selected_emergency).pack(side="left", padx=8)
        ttk.Button(bar, text="Sync Event Log queue", style="Secondary.TButton", command=self.sync_eventlog_queue).pack(side="left")

        cols = ("date", "event", "photographer", "photos", "status", "dropbox", "eventlog", "sd")
        self.history_tree = ttk.Treeview(card, columns=cols, show="headings")
        headings = {
            "date": ("Date / time", 150), "event": ("Event", 180), "photographer": ("Photographer", 150),
            "photos": ("Photos", 80), "status": ("Local backup", 190), "dropbox": ("Dropbox", 125), "eventlog": ("Event Log", 120), "sd": ("SD cleared", 90)
        }
        for c in cols:
            text, width = headings[c]
            self.history_tree.heading(c, text=text)
            self.history_tree.column(c, width=width, anchor="w")
        self.history_tree.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 16))

    def refresh_history(self):
        if not hasattr(self, "history_tree"):
            return
        self.history_tree.delete(*self.history_tree.get_children())
        for row in self.storage.list_jobs():
            status = row["status"].replace("_", " ").title()
            self.history_tree.insert("", "end", iid=row["id"], values=(row["created_at"][:16].replace("T", " "), f"{row['event_name']} {row['event_time']}", row["photographer"], row["selected_count"], status, row["backup3_status"].replace("_", " ").title(), row["eventlog_status"], "Yes" if row["sd_deleted"] else "No"))

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
        results = self.engine.eventlog.sync_all()
        ok = sum(1 for _, success, _ in results if success)
        messagebox.showinfo("Event Log sync", f"{ok} queued record(s) sent successfully.\n{len(results)-ok} remain queued/not configured.", parent=self)
        self.refresh_history()

    # ---------------- Admin ----------------
    def build_admin_page(self):
        page = self.pages["admin"]
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)
        head = tk.Frame(page, bg=BG)
        head.grid(row=0, column=0, sticky="ew", padx=34, pady=(25, 14))
        ttk.Label(head, text="Admin", style="Title.TLabel").pack(anchor="w")
        ttk.Label(head, text="Locked local backup destinations, Dropbox cloud backup, emergency storage and system configuration.", style="Subtitle.TLabel").pack(anchor="w", pady=(3, 0))

        canvas = tk.Canvas(page, bg=BG, highlightthickness=0)
        canvas.grid(row=1, column=0, sticky="nsew", padx=34, pady=(0, 24))
        sb = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
        sb.grid(row=1, column=1, sticky="ns", pady=(0,24))
        canvas.configure(yscrollcommand=sb.set)
        inner = tk.Frame(canvas, bg=BG)
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
        tk.Label(syscard, text="EVENT LOG & CARD CLEARING", bg=CARD, fg=GOLD, font=("Segoe UI Semibold", 9)).grid(row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(14,8))
        tk.Label(syscard, text="Event Log API URL", bg=CARD, fg=INK, font=("Segoe UI Semibold", 9)).grid(row=1, column=0, sticky="w", padx=16, pady=7)
        self.eventlog_url_var = tk.StringVar()
        ttk.Entry(syscard, textvariable=self.eventlog_url_var).grid(row=1, column=1, sticky="ew", padx=(8,16), pady=7)
        tk.Label(syscard, text="Where completed event records are sent. Leave blank for now and records remain safely queued locally.", bg=CARD, fg=MUTED, font=("Segoe UI", 8), wraplength=760, justify="left").grid(row=2, column=1, sticky="w", padx=(8,16), pady=(0,7))
        tk.Label(syscard, text="Event Log API key", bg=CARD, fg=INK, font=("Segoe UI Semibold", 9)).grid(row=3, column=0, sticky="w", padx=16, pady=7)
        self.eventlog_key_var = tk.StringVar()
        ttk.Entry(syscard, textvariable=self.eventlog_key_var, show="•").grid(row=3, column=1, sticky="ew", padx=(8,16), pady=7)
        tk.Label(syscard, text="Security key used by Event Log to accept imports. Leave blank until the Event Log endpoint is enabled.", bg=CARD, fg=MUTED, font=("Segoe UI", 8), wraplength=760, justify="left").grid(row=4, column=1, sticky="w", padx=(8,16), pady=(0,7))
        self.admin_delete_default_var = tk.BooleanVar()
        ttk.Checkbutton(syscard, text="Pre-select 'Delete imported SD files after 2 local backups are verified'", variable=self.admin_delete_default_var).grid(row=5, column=0, columnspan=2, sticky="w", padx=16, pady=(7,4))
        tk.Label(syscard, text="Even when pre-selected, staff still receive a final confirmation. Dropbox is not required before the SD card can be cleared.", bg=CARD, fg=MUTED, font=("Segoe UI", 8), wraplength=800, justify="left").grid(row=6, column=0, columnspan=2, sticky="w", padx=16, pady=(0,14))

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
        if hasattr(self, "eventlog_url_var"):
            self.eventlog_url_var.set(self.storage.get_setting("eventlog_url", ""))
            self.eventlog_key_var.set(self.storage.get_setting("eventlog_api_key", ""))
            self.admin_delete_default_var.set(bool(self.storage.get_setting("delete_after_verified_default", False)))
            self.dropbox_app_key_var.set(self.storage.get_setting("dropbox_app_key", ""))
            self.refresh_dropbox_admin_status()
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
        self.storage.set_setting("eventlog_url", self.eventlog_url_var.get().strip())
        self.storage.set_setting("eventlog_api_key", self.eventlog_key_var.get().strip())
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
        msg = "Exit the application?"
        if cloud_running:
            msg = "A Dropbox upload is still in progress.\n\nYou can still exit safely: both local backups are already verified and the Dropbox upload will resume next time the app starts.\n\nExit now?"
        if messagebox.askokcancel("Exit TPS Photo Import", msg, parent=self):
            self.destroy()
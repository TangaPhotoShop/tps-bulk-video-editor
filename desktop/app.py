from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import BooleanVar, DoubleVar, IntVar, StringVar, Text, Tk, Toplevel, filedialog, messagebox, ttk

import imageio_ffmpeg
from PIL import Image, ImageTk

APP_NAME = "TPS Bulk Video Editor"
APP_VERSION = "1.3.14"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi"}


def clean_code(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", value.strip()).strip("-").upper()
    return cleaned or fallback


def ffmpeg_path() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def human_size(byte_count: int) -> str:
    value = float(max(0, byte_count))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return "0 B"


@dataclass
class Clip:
    source: Path
    selected: BooleanVar
    status: StringVar
    progress: DoubleVar
    full_size: StringVar
    low_size: StringVar


class TPSVideoEditor:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title(f"{APP_NAME} — Version {APP_VERSION}")
        self.root.geometry("1280x820")
        self.root.minsize(1000, 680)
        self.root.configure(bg="#eef3f4")
        self.clips: list[Clip] = []
        self.preview_image = None
        self.app_icon_image = None
        self.processing = False
        self.stop_requested = threading.Event()
        self.active_process = None
        self.preview_job = None
        self.preview_generation = 0
        self.preview_duration_cache = {}
        self.last_output_folder = None
        self.export_started_at = None

        self.source = StringVar()
        self.destination = StringVar()
        self.job_date = StringVar(value=datetime.now().strftime("%d-%b-%Y").upper())
        self.activity = StringVar(value="DOL")
        self.photographer = StringVar(value="DM")
        self.start_number = IntVar(value=1)
        self.folder_name = StringVar(value="TPS Edited Videos")
        self.first_filename = StringVar(value=f"{self.job_date.get()}-DOL-DM-0001.MP4")
        self.low_res_enabled = BooleanVar(value=False)
        self.low_destination = StringVar()
        self.low_resolution = StringVar(value="640x480")
        self.export_quality = DoubleVar(value=85.0)
        self.auto_correct = BooleanVar(value=False)
        self.auto_white_balance = BooleanVar(value=False)
        self.exposure = DoubleVar(value=0.0)
        self.contrast = DoubleVar(value=1.0)
        self.shadows = DoubleVar(value=0.0)
        self.whites = DoubleVar(value=0.0)
        self.highlights = DoubleVar(value=0.0)
        self.blacks = DoubleVar(value=0.0)
        self.white_balance = DoubleVar(value=0.0)
        self.warmth = DoubleVar(value=0.0)
        self.saturation = DoubleVar(value=1.0)
        self.volume = DoubleVar(value=1.0)
        self.logo_enabled = BooleanVar(value=True)
        self.logo_path = StringVar(value=str(Path(__file__).with_name("assets") / "tps-logo.png"))
        self.logo_size = StringVar(value="15%")
        self.preview_selected_name = StringVar()
        self.preview_timeline = DoubleVar(value=15)
        self.preview_status = StringVar(value="Choose a video folder to begin previewing.")

        self._style()
        self._build()

    def _style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#eef3f4")
        style.configure("Card.TFrame", background="white")
        style.configure("TLabel", background="#eef3f4", foreground="#17343b", font=("Segoe UI", 10))
        style.configure("Card.TLabel", background="white")
        style.configure("Title.TLabel", background="#073f49", foreground="white", font=("Segoe UI Semibold", 22))
        style.configure("Primary.TButton", font=("Segoe UI Semibold", 10), padding=(14, 9))
        style.configure("Busy.TLabel", background="#ef7d00", foreground="white", font=("Segoe UI Semibold", 11), padding=(10, 8))
        style.configure("TButton", padding=(10, 7))
        style.configure("TLabelframe", background="white", padding=12)
        style.configure("TLabelframe.Label", background="white", foreground="#073f49", font=("Segoe UI Semibold", 11))

    def _build(self):
        header = ttk.Frame(self.root, padding=(20, 14), style="Card.TFrame")
        header.pack(fill="x")
        app_icon_path = Path(__file__).with_name("assets") / "app-icon.png"
        if app_icon_path.exists():
            icon = Image.open(app_icon_path)
            icon.thumbnail((64, 64), Image.Resampling.LANCZOS)
            self.app_icon_image = ImageTk.PhotoImage(icon)
            self.root.iconphoto(True, self.app_icon_image)
            ttk.Label(header, image=self.app_icon_image, style="Card.TLabel").pack(side="left", padx=(0, 10))
        ttk.Label(header, text=f"TPS BULK VIDEO EDITOR  •  VERSION {APP_VERSION}", style="Title.TLabel", padding=(14, 8)).pack(side="left")
        ttk.Label(header, text="SD card → adjust → rename → new output folder", style="Card.TLabel").pack(side="left", padx=18)
        ttk.Button(header, text="Staff instructions", command=self.show_instructions).pack(side="right")

        body = ttk.Frame(self.root, padding=16)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        right = ttk.Frame(body, width=390)
        right.pack(side="right", fill="y", padx=(8, 0))

        files = ttk.LabelFrame(left, text="1. Videos from SD card")
        files.pack(fill="x")
        source_row = ttk.Frame(files, style="Card.TFrame")
        source_row.pack(fill="x", pady=(0, 8))
        ttk.Entry(source_row, textvariable=self.source).pack(side="left", fill="x", expand=True)
        ttk.Button(source_row, text="Choose SD card / folder", command=self.choose_source).pack(side="left", padx=(8, 0))
        actions = ttk.Frame(files, style="Card.TFrame")
        actions.pack(fill="x", pady=(0, 8))
        ttk.Button(actions, text="Select all", command=lambda: self.set_all(True)).pack(side="left")
        ttk.Button(actions, text="Select none", command=lambda: self.set_all(False)).pack(side="left", padx=6)
        ttk.Button(actions, text="Preview selected", command=self.preview_selected).pack(side="left")
        self.count_label = ttk.Label(actions, text="0 videos", style="Card.TLabel")
        self.count_label.pack(side="right")

        columns = ("use", "source", "original_size", "output", "new_size", "low_size", "status")
        tree_frame = ttk.Frame(files, style="Card.TFrame")
        tree_frame.pack(fill="both", expand=True)
        tree_scroll_y = ttk.Scrollbar(tree_frame, orient="vertical")
        tree_scroll_x = ttk.Scrollbar(tree_frame, orient="horizontal")
        self.tree = ttk.Treeview(
            tree_frame, columns=columns, show="headings", selectmode="browse", height=7,
            yscrollcommand=tree_scroll_y.set, xscrollcommand=tree_scroll_x.set,
        )
        tree_scroll_y.config(command=self.tree.yview)
        tree_scroll_x.config(command=self.tree.xview)
        self.tree.heading("use", text="Use")
        self.tree.heading("source", text="Source file")
        self.tree.heading("original_size", text="Original size")
        self.tree.heading("output", text="New filename")
        self.tree.heading("new_size", text="New size")
        self.tree.heading("low_size", text="Low-res size")
        self.tree.heading("status", text="Status")
        self.tree.column("use", width=52, anchor="center")
        self.tree.column("source", width=220)
        self.tree.column("original_size", width=90, anchor="e")
        self.tree.column("output", width=250)
        self.tree.column("new_size", width=85, anchor="e")
        self.tree.column("low_size", width=90, anchor="e")
        self.tree.column("status", width=120)
        tree_scroll_y.pack(side="right", fill="y")
        tree_scroll_x.pack(side="bottom", fill="x")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Button-1>", self.toggle_row)
        self.tree.bind("<<TreeviewSelect>>", self.preview_tree_selected)

        preview = ttk.LabelFrame(left, text="2. Live before and after preview")
        preview.pack(fill="both", expand=True, pady=(10, 0))
        preview_top = ttk.Frame(preview, style="Card.TFrame")
        preview_top.pack(fill="x", pady=(0, 6))
        ttk.Label(preview_top, text="Video", style="Card.TLabel").pack(side="left")
        self.preview_chooser = ttk.Combobox(preview_top, textvariable=self.preview_selected_name, state="readonly", width=52)
        self.preview_chooser.pack(side="left", fill="x", expand=True, padx=8)
        self.preview_chooser.bind("<<ComboboxSelected>>", lambda _e: self._schedule_preview(immediate=True))
        ttk.Label(preview_top, textvariable=self.preview_status, style="Card.TLabel").pack(side="right")
        preview_images = ttk.Frame(preview, style="Card.TFrame")
        preview_images.pack(fill="both", expand=True)
        self.preview_before = ttk.Label(preview_images, text="BEFORE", anchor="center", style="Card.TLabel")
        self.preview_before.pack(side="left", fill="both", expand=True, padx=(0, 5))
        self.preview_after = ttk.Label(preview_images, text="AFTER — current export settings", anchor="center", style="Card.TLabel")
        self.preview_after.pack(side="left", fill="both", expand=True, padx=(5, 0))
        ttk.Scale(preview, variable=self.preview_timeline, from_=0, to=100, command=lambda _v: self._schedule_preview()).pack(fill="x", pady=(7, 0))
        ttk.Label(preview, text="Move through the video or adjust any control—the preview refreshes automatically.", style="Card.TLabel").pack(pady=(4, 0))

        naming = ttk.LabelFrame(right, text="3. Rename and output")
        naming.pack(fill="x")
        self._field(naming, "First output filename", self.first_filename)
        ttk.Label(naming, text="Example: 14-SEP-2026-DOL-SW-001.MP4", style="Card.TLabel").pack(anchor="w", pady=(0, 4))
        folder_row = ttk.Frame(naming, style="Card.TFrame")
        folder_row.pack(fill="x", pady=3)
        ttk.Label(folder_row, text="New folder name", width=19, style="Card.TLabel").pack(side="left")
        ttk.Label(folder_row, textvariable=self.folder_name, style="Card.TLabel", wraplength=210).pack(side="left", fill="x", expand=True)
        dest_row = ttk.Frame(naming, style="Card.TFrame")
        dest_row.pack(fill="x", pady=4)
        ttk.Entry(dest_row, textvariable=self.destination).pack(side="left", fill="x", expand=True)
        ttk.Button(dest_row, text="Destination", command=self.choose_destination).pack(side="left", padx=(6, 0))
        low_toggle = ttk.Frame(naming, style="Card.TFrame")
        low_toggle.pack(fill="x", pady=(7, 2))
        ttk.Checkbutton(low_toggle, text="Also create low-res watermarked copies", variable=self.low_res_enabled).pack(side="left")
        ttk.Combobox(low_toggle, textvariable=self.low_resolution, values=("640x480", "854x480", "1280x720"), state="readonly", width=10).pack(side="right")
        low_row = ttk.Frame(naming, style="Card.TFrame")
        low_row.pack(fill="x", pady=3)
        ttk.Entry(low_row, textvariable=self.low_destination).pack(side="left", fill="x", expand=True)
        ttk.Button(low_row, text="Low-res destination", command=self.choose_low_destination).pack(side="left", padx=(6, 0))
        ttk.Label(naming, text="Folder: filename prefix + -low res", style="Card.TLabel").pack(anchor="w")
        quality_row = ttk.Frame(naming, style="Card.TFrame")
        quality_row.pack(fill="x", pady=(8, 2))
        ttk.Label(quality_row, text="Export quality", width=15, style="Card.TLabel").pack(side="left")
        quality_value = ttk.Label(quality_row, text="85%", width=5, style="Card.TLabel")
        quality_value.pack(side="right")
        ttk.Scale(quality_row, variable=self.export_quality, from_=0, to=100, command=lambda _v: quality_value.config(text=f"{self.export_quality.get():.0f}%")).pack(side="left", fill="x", expand=True)
        ttk.Label(naming, text="85% default • 70–100 recommended • lower = smaller file", style="Card.TLabel").pack(anchor="w")

        edits = ttk.LabelFrame(right, text="4. Bulk adjustments")
        edits.pack(fill="x", pady=10)
        self._slider(edits, "Exposure", self.exposure, -1.5, 1.5)
        self._slider(edits, "Contrast", self.contrast, 0.7, 1.4)
        self._slider(edits, "Shadows", self.shadows, -1.0, 1.0)
        self._slider(edits, "Highlights", self.highlights, -1.0, 1.0)
        self._slider(edits, "Blacks", self.blacks, -1.0, 1.0)
        self._slider(edits, "Whites", self.whites, -1.0, 1.0)
        self._slider(edits, "White balance", self.white_balance, -1.0, 1.0)
        self._slider(edits, "Warmth", self.warmth, -0.3, 0.3)
        self._slider(edits, "Saturation", self.saturation, 0.0, 2.0)
        self._slider(edits, "Volume", self.volume, 0.0, 4.0)
        auto_row = ttk.Frame(edits, style="Card.TFrame")
        auto_row.pack(fill="x", pady=(7, 0))
        ttk.Checkbutton(auto_row, text="Auto Exposure", variable=self.auto_correct, command=self._adjustment_changed).pack(side="left")
        ttk.Checkbutton(auto_row, text="Auto White Balance", variable=self.auto_white_balance, command=self._adjustment_changed).pack(side="left", padx=8)
        preset_row = ttk.Frame(edits, style="Card.TFrame")
        preset_row.pack(fill="x", pady=(7, 0))
        ttk.Button(preset_row, text="Reset adjustments", command=self.neutral).pack(side="left")

        logo = ttk.LabelFrame(right, text="5. TPS logo")
        logo.pack(fill="x")
        logo_controls = ttk.Frame(logo, style="Card.TFrame")
        logo_controls.pack(fill="x")
        ttk.Checkbutton(logo_controls, text="Add TPS logo — top left", variable=self.logo_enabled, command=self._adjustment_changed).pack(side="left")
        logo_picker = ttk.Combobox(logo_controls, textvariable=self.logo_size, values=("10%", "15%", "20%"), state="readonly", width=6)
        logo_picker.pack(side="right")
        logo_picker.bind("<<ComboboxSelected>>", lambda _e: self._adjustment_changed())
        ttk.Label(logo, text="Defaults to 15% • aspect ratio preserved • fixed drop shadow", style="Card.TLabel", wraplength=340).pack(anchor="w", pady=(4, 0))

        self.run_button = ttk.Button(right, text="CREATE EDITED VIDEOS", style="Primary.TButton", command=self.start_processing)
        self.run_button.pack(fill="x", pady=(12, 4))
        self.busy_notice = ttk.Label(right, text="READY TO EXPORT", anchor="center", style="Card.TLabel")
        self.busy_notice.pack(fill="x", pady=(2, 4))
        self.stop_button = ttk.Button(right, text="STOP EXPORTING", command=self.request_stop)
        self.stop_button.pack(fill="x", pady=(0, 4))
        self.stop_button.state(["disabled"])
        self.open_folder_button = ttk.Button(right, text="OPEN OUTPUT FOLDER", command=self.open_output_folder)
        self.open_folder_button.pack(fill="x", pady=(0, 4))
        self.open_folder_button.state(["disabled"])
        self.overall = ttk.Progressbar(right, maximum=100)
        self.overall.pack(fill="x")
        self.summary = ttk.Label(right, text="Original SD-card files are never changed.", wraplength=360)
        self.summary.pack(fill="x", pady=7)

        self.first_filename.trace_add("write", lambda *_: self.filename_changed())

    def _field(self, parent, label, variable):
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=label, width=19, style="Card.TLabel").pack(side="left")
        ttk.Entry(row, textvariable=variable).pack(side="left", fill="x", expand=True)

    def _slider(self, parent, label, variable, low, high):
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", pady=2)
        value = ttk.Label(row, width=6, style="Card.TLabel")
        value.pack(side="right")
        ttk.Label(row, text=label, width=14, style="Card.TLabel").pack(side="left")
        scale = ttk.Scale(row, variable=variable, from_=low, to=high)
        scale.pack(side="left", fill="x", expand=True)
        value.config(text=f"{variable.get():.2f}")
        variable.trace_add("write", lambda *_args, v=variable, label=value: self._slider_changed(v, label))

    def _slider_changed(self, variable, value_label):
        value_label.config(text=f"{variable.get():.2f}")
        self._adjustment_changed()

    def _adjustment_changed(self):
        self._schedule_preview()

    def show_instructions(self):
        win = Toplevel(self.root)
        win.title(f"Staff Instructions — {APP_NAME} Version {APP_VERSION}")
        win.geometry("900x720")
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="TPS BULK VIDEO EDITOR — STAFF INSTRUCTIONS", font=("Segoe UI Semibold", 18)).pack(anchor="w", pady=(0, 10))
        text_frame = ttk.Frame(frame)
        text_frame.pack(fill="both", expand=True)
        scroll = ttk.Scrollbar(text_frame)
        scroll.pack(side="right", fill="y")
        guide = Text(text_frame, wrap="word", yscrollcommand=scroll.set, font=("Segoe UI", 11), padx=14, pady=12, background="white", foreground="#17343b")
        guide.pack(fill="both", expand=True)
        scroll.config(command=guide.yview)
        guide.insert("1.0", f"""QUICK WORKFLOW

1. Select Choose SD card / folder and open the folder containing the videos.
2. Tick the videos required. Select all and Select none change the whole list.
3. Enter the first complete output filename, for example 14-SEP-2026-DOL-SW-0001.MP4.
4. Choose the destination where the new output folder will be created.
5. Select corrections, low-resolution copies and TPS logo settings as required.
6. Select Preview selected and move through the timeline to compare BEFORE and AFTER.
7. Select CREATE EDITED VIDEOS and watch the live progress display.

VIDEOS AND NAMING

First output filename — Controls every selected filename and the new folder name. The final number increases automatically: 0001, 0002, 0003 and so on.
New folder name — Created automatically from the filename prefix and cannot conflict with an existing completed folder.
Destination — Parent location for the full-resolution output folder.

LOW-RESOLUTION COPIES

Also create low-res watermarked copies — Creates a second upload-ready batch with the same filenames.
Resolution — 640x480, 854x480 or 1280x720. The image is never stretched; padding is added when required.
Low-res destination — Optional separate location. Its folder name ends with -low res.
Export quality — Controls video compression from 0 to 100 without changing the full-resolution dimensions. Default: 85%. Higher values create larger, cleaner files; 70–100 is recommended. Exact file size depends on the footage.

FILE SIZES AND OUTPUT

Original size — Shown before export for each source video.
New size and Low-res size — Update while each output is being written and show the exact final sizes after completion.
Free-space check — Before starting, the app checks that the full-resolution destination has enough estimated free space.
Open Output Folder — Becomes available after an export finishes, stops or encounters an error, so completed files can be reached immediately.

BULK ADJUSTMENTS

Exposure — Brightens or darkens the image. Default: 0.00.
Contrast — Changes the difference between dark and bright areas. Default: 1.00.
Shadows — Fine-tunes detail in darker areas without moving the main exposure. Default: 0.00.
Highlights — Fine-tunes detail in bright areas. Default: 0.00.
Blacks — Lifts or deepens the darkest point. Default: 0.00.
Whites — Fine-tunes the brightest white point. Default: 0.00.
White balance — Corrects an overall blue or amber colour cast. Default: 0.00.
Warmth — Adds warmer orange tones or cooler blue tones. Default: 0.00.
Saturation — Controls colour intensity. Default: 1.00.
Volume — 0 is silent, 1 is original volume, and up to 4 boosts very quiet nights. Boosted audio is peak-limited to reduce clipping. Default: 1.00.
Auto Exposure — Analyses and adjusts exposure continuously through the video. Changes are smoothed over neighbouring frames to prevent flicker or sudden brightness pumping. Default: OFF.
Auto White Balance — Applies a conservative colour correction through the full video. It is designed to preserve skin tones and resist sudden yellow/green shifts in night footage. Default: OFF.
Reset adjustments — Restores every correction slider and automatic option to its startup default.

TPS LOGO

Add TPS logo — Adds the locked TPS logo at the top left with its original proportions and a drop shadow. Default: ON.
Logo size — Safe choices are 10%, 15% and 20% of video width. Default on every launch: 15%.

PREVIEW

The permanent BEFORE/AFTER viewer is part of the main window. Select a video, move its timeline, or change any adjustment and the AFTER image refreshes automatically using the same settings as export.

PROGRESS AND SAFETY

The orange BUSY notice, status column, progress bar and estimated time remaining show the current export. Stop Exporting asks for confirmation, keeps completed videos and removes the incomplete file currently being written. Original SD-card files are never modified or deleted.

STARTUP DEFAULTS — VERSION {APP_VERSION}

Auto Exposure OFF | Auto White Balance OFF | Exposure 0.00 | Contrast 1.00 | Shadows 0.00 | Highlights 0.00 | Blacks 0.00 | Whites 0.00 | White Balance 0.00 | Warmth 0.00 | Saturation 1.00 | Volume 1.00 | Export quality 85% | Low-resolution copies OFF | TPS logo ON | Logo size 15%
""")
        guide.config(state="disabled")
        ttk.Button(frame, text="Close instructions", command=win.destroy).pack(pady=(10, 0))

    def choose_source(self):
        path = filedialog.askdirectory(title="Choose the SD card or video folder")
        if path:
            self.source.set(path)
            if not self.destination.get():
                self.destination.set(str(Path.home() / "Videos"))
            if not self.low_destination.get():
                self.low_destination.set(self.destination.get())
            self.load_clips(Path(path))

    def choose_destination(self):
        path = filedialog.askdirectory(title="Choose where the new folder will be created")
        if path:
            self.destination.set(path)
            if not self.low_destination.get():
                self.low_destination.set(path)

    def choose_low_destination(self):
        path = filedialog.askdirectory(title="Choose where the low-resolution folder will be created")
        if path:
            self.low_destination.set(path)

    def load_clips(self, folder: Path):
        paths = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS)
        self.clips = [Clip(p, BooleanVar(value=True), StringVar(value="Ready"), DoubleVar(value=0), StringVar(value="—"), StringVar(value="—")) for p in paths]
        names = [p.name for p in paths]
        self.preview_chooser["values"] = names
        self.preview_selected_name.set(names[0] if names else "")
        self.refresh_tree()
        if names:
            self._schedule_preview(immediate=True)

    def filename_parts(self):
        value = Path(self.first_filename.get().strip()).name
        if not value.lower().endswith(".mp4"):
            value += ".MP4"
        stem = Path(value).stem
        match = re.match(r"^(.*?)[-_](\d+)$", stem)
        if match:
            prefix, number = match.groups()
            return clean_code(prefix, "TPS-VIDEO"), int(number), max(3, len(number))
        return clean_code(stem, "TPS-VIDEO"), 1, 4

    def filename_changed(self):
        prefix, _, _ = self.filename_parts()
        self.folder_name.set(prefix)
        self.refresh_tree()

    def output_name(self, index: int) -> str:
        prefix, start, width = self.filename_parts()
        return f"{prefix}-{start + index:0{width}d}.MP4"

    def refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        selected_index = 0
        for index, clip in enumerate(self.clips):
            if clip.selected.get():
                name = self.output_name(selected_index)
                selected_index += 1
            else:
                name = "—"
            try:
                original_size = human_size(clip.source.stat().st_size)
            except OSError:
                original_size = "Unavailable"
            self.tree.insert("", "end", iid=str(index), values=("✓" if clip.selected.get() else "", clip.source.name, original_size, name, clip.full_size.get(), clip.low_size.get(), clip.status.get()))
        self.count_label.config(text=f"{selected_index} of {len(self.clips)} videos selected")

    def toggle_row(self, event):
        if self.tree.identify_region(event.x, event.y) == "cell" and self.tree.identify_column(event.x) == "#1":
            row = self.tree.identify_row(event.y)
            if row:
                clip = self.clips[int(row)]
                clip.selected.set(not clip.selected.get())
                self.refresh_tree()

    def set_all(self, value: bool):
        for clip in self.clips:
            clip.selected.set(value)
        self.refresh_tree()

    def neutral(self):
        self.auto_correct.set(False)
        self.auto_white_balance.set(False)
        for var, value in [(self.exposure, 0), (self.contrast, 1), (self.shadows, 0), (self.highlights, 0), (self.blacks, 0), (self.whites, 0), (self.white_balance, 0), (self.warmth, 0), (self.saturation, 1), (self.volume, 1)]:
            var.set(value)
        self._adjustment_changed()

    def selected_clips(self):
        return [c for c in self.clips if c.selected.get()]

    def preview_selected(self):
        clips = self.selected_clips()
        if not clips:
            return messagebox.showinfo(APP_NAME, "Select a video first.")
        self.preview_selected_name.set(clips[0].source.name)
        self._schedule_preview(immediate=True)

    def preview_tree_selected(self, _event=None):
        selection = self.tree.selection()
        if selection:
            self.preview_selected_name.set(self.clips[int(selection[0])].source.name)
            self._schedule_preview(immediate=True)

    def _schedule_preview(self, _value=None, immediate=False):
        if not self.preview_selected_name.get() or not self.clips:
            return
        if self.preview_job is not None:
            self.root.after_cancel(self.preview_job)
        self.preview_job = self.root.after(0 if immediate else 250, self._start_preview_render)

    def _start_preview_render(self):
        source = next((c.source for c in self.clips if c.source.name == self.preview_selected_name.get()), None)
        if source is None:
            return
        self.preview_status.set("Updating preview…")
        self.preview_generation += 1
        generation = self.preview_generation
        settings = self.settings_snapshot()
        threading.Thread(target=self._render_preview_pair, args=(source, self.preview_timeline.get(), generation, settings), daemon=True).start()

    def _render_preview_pair(self, source: Path, percent: float, generation: int, settings):
        duration = self.video_duration(source)
        timestamp = max(0, duration * percent / 100.0)
        token = re.sub(r"\W+", "-", source.stem)[:35]
        temp = Path(tempfile.gettempdir())
        before_path = temp / f"tps-before-{token}-{generation}.jpg"
        after_path = temp / f"tps-after-{token}-{generation}.jpg"
        common = [ffmpeg_path(), "-y", "-ss", f"{timestamp:.3f}", "-i", str(source)]
        self.run_hidden(common + ["-frames:v", "1", "-vf", "scale=520:-2", str(before_path)], capture_output=True)
        self.run_hidden(self.preview_frame_command(source, timestamp, after_path, settings), capture_output=True)
        if not before_path.exists() or not after_path.exists():
            self.root.after(0, lambda: self.preview_status.set("Preview could not be created"))
            return
        def show():
            if generation != self.preview_generation:
                return
            before_image = Image.open(before_path); before_image.thumbnail((520, 580))
            after_image = Image.open(after_path); after_image.thumbnail((520, 580))
            before_photo = ImageTk.PhotoImage(before_image)
            after_photo = ImageTk.PhotoImage(after_image)
            self.preview_before.config(image=before_photo, text="")
            self.preview_after.config(image=after_photo, text="")
            self.preview_before.image = before_photo
            self.preview_after.image = after_photo
            self.preview_status.set(f"{timestamp:.1f}s of {duration:.1f}s")
        self.root.after(0, show)

    def video_duration(self, source: Path) -> float:
        cached = self.preview_duration_cache.get(source)
        if cached is not None:
            return cached
        proc = self.run_hidden([ffmpeg_path(), "-i", str(source)], capture_output=True, text=True)
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
        if not match:
            return 1.0
        hours, minutes, seconds = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        self.preview_duration_cache[source] = duration
        return duration

    def video_dimensions(self, source: Path) -> tuple[int, int]:
        proc = self.run_hidden([ffmpeg_path(), "-i", str(source)], capture_output=True, text=True)
        match = re.search(r"Video:.*?\b(\d{2,5})x(\d{2,5})\b", proc.stderr)
        return (int(match.group(1)), int(match.group(2))) if match else (1920, 1080)

    @staticmethod
    def run_hidden(command, **kwargs):
        # Prevent FFmpeg from opening a black console window in the Windows GUI app.
        if os.name == "nt":
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kwargs["startupinfo"] = startupinfo
        return subprocess.run(command, **kwargs)

    @staticmethod
    def popen_hidden(command, **kwargs):
        if os.name == "nt":
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kwargs["startupinfo"] = startupinfo
        return subprocess.Popen(command, **kwargs)

    def run_export(self, command, duration: float, clip: Clip, stage: str, task_index: int, task_total: int):
        progress_command = command[:-1] + ["-progress", "pipe:1", "-nostats", command[-1]]
        proc = self.popen_hidden(progress_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        self.active_process = proc
        self.root.after(0, lambda s=stage, n=clip.source.name: self.busy_notice.config(text=f"BUSY — {s.upper()}\n{n}", style="Busy.TLabel"))
        recent = deque(maxlen=80)
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            recent.append(line)
            if line.startswith(("out_time_ms=", "out_time_us=")):
                try:
                    seconds = int(line.split("=", 1)[1]) / 1_000_000
                    current_percent = min(100.0, seconds / max(duration, 0.1) * 100)
                    overall_percent = ((task_index + current_percent / 100.0) / task_total) * 100
                    target = Path(command[-1])
                    current_size = human_size(target.stat().st_size) if target.exists() else "0 B"
                    size_var = clip.low_size if stage.startswith("Low") else clip.full_size
                    elapsed = max(0.1, time.monotonic() - self.export_started_at) if self.export_started_at else 0.1
                    remaining = max(0.0, elapsed * (100.0 - overall_percent) / max(overall_percent, 0.1))
                    eta_text = f" • about {int(remaining // 60)}m {int(remaining % 60)}s remaining" if overall_percent >= 1 else ""
                    self.root.after(0, lambda p=overall_percent: self.overall.configure(value=p))
                    self.root.after(0, lambda v=size_var, s=current_size: v.set(s))
                    self.root.after(0, lambda c=clip, s=stage, p=current_percent: c.status.set(f"{s} {p:.0f}%"))
                    self.root.after(0, lambda s=stage, n=clip.source.name, p=current_percent: self.busy_notice.config(text=f"BUSY — {s.upper()} {p:.0f}%\n{n}", style="Busy.TLabel"))
                    self.root.after(0, lambda p=overall_percent, e=eta_text: self.summary.config(text=f"Overall progress: {p:.0f}%{e}"))
                    self.root.after(0, self.refresh_tree)
                except ValueError:
                    pass
        code = proc.wait()
        self.active_process = None
        return code, "\n".join(recent)

    def request_stop(self):
        if not self.processing:
            return
        if not messagebox.askyesno(APP_NAME, "Stop exporting now?\n\nCompleted videos will be kept. The incomplete video currently being created will be removed."):
            return
        self.stop_requested.set()
        self.stop_button.state(["disabled"])
        self.busy_notice.config(text="STOPPING EXPORT SAFELY…", style="Busy.TLabel")
        self.summary.config(text="Stopping after the current FFmpeg process closes…")
        proc = self.active_process
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def open_output_folder(self):
        if self.last_output_folder and Path(self.last_output_folder).exists():
            if os.name == "nt":
                os.startfile(str(self.last_output_folder))
            else:
                subprocess.Popen(["xdg-open", str(self.last_output_folder)])

    def validate(self):
        if not self.selected_clips():
            return "Select at least one video."
        if not self.destination.get():
            return "Choose an output destination."
        if self.low_res_enabled.get() and not (self.low_destination.get() or self.destination.get()):
            return "Choose a destination for the low-resolution copies."
        if (self.logo_enabled.get() or self.low_res_enabled.get()) and not Path(self.logo_path.get()).exists():
            return "Choose the TPS logo PNG, or turn the logo off."
        return None

    def start_processing(self):
        problem = self.validate()
        if problem:
            return messagebox.showerror(APP_NAME, problem)
        if self.processing:
            return
        self.summary.config(text="Preparing output folders…")
        self.root.update_idletasks()
        try:
            destination_parent = Path(self.destination.get())
            destination_parent.mkdir(parents=True, exist_ok=True)
            free_space = shutil.disk_usage(destination_parent).free
            selected_bytes = sum(c.source.stat().st_size for c in self.selected_clips())
            required = selected_bytes * (1.25 if self.low_res_enabled.get() else 1.1)
            if free_space < required:
                return messagebox.showerror(APP_NAME, f"There may not be enough free space at the destination.\n\nEstimated requirement: {human_size(int(required))}\nAvailable: {human_size(free_space)}")
            output = self.create_unique_folder(Path(self.destination.get()), clean_code(self.folder_name.get(), "TPS-Edited-Videos"))
            low_output = None
            if self.low_res_enabled.get():
                low_parent = Path(self.low_destination.get() or self.destination.get())
                low_output = self.create_unique_folder(low_parent, f"{clean_code(self.folder_name.get(), 'TPS-Edited-Videos')}-low res")
        except OSError as exc:
            self.summary.config(text="Could not create the output folder.")
            return messagebox.showerror(APP_NAME, f"The output folder could not be created.\n\n{exc}")
        self.processing = True
        self.export_started_at = time.monotonic()
        self.last_output_folder = output
        self.open_folder_button.state(["disabled"])
        self.stop_requested.clear()
        self.run_button.state(["disabled"])
        self.stop_button.state(["!disabled"])
        self.busy_notice.config(text="BUSY — PREPARING EXPORT…", style="Busy.TLabel")
        self.summary.config(text="Starting video processing…")
        self.overall.configure(value=0)
        settings = self.settings_snapshot()
        selected = self.selected_clips()
        output_names = [self.output_name(index) for index in range(len(selected))]
        threading.Thread(target=self.process_batch, args=(output, low_output, settings, selected, output_names), daemon=True).start()

    @staticmethod
    def create_unique_folder(parent: Path, requested_name: str) -> Path:
        parent.mkdir(parents=True, exist_ok=True)
        candidate = parent / requested_name
        suffix = 2
        while candidate.exists():
            candidate = parent / f"{requested_name}-{suffix}"
            suffix += 1
        candidate.mkdir(parents=False, exist_ok=False)
        return candidate

    def settings_snapshot(self):
        """Copy every Tk value on the UI thread before background work starts."""
        return {
            "auto_correct": self.auto_correct.get(),
            "auto_white_balance": self.auto_white_balance.get(),
            "exposure": self.exposure.get(),
            "contrast": self.contrast.get(),
            "shadows": self.shadows.get(),
            "highlights": self.highlights.get(),
            "blacks": self.blacks.get(),
            "whites": self.whites.get(),
            "white_balance": self.white_balance.get(),
            "warmth": self.warmth.get(),
            "saturation": self.saturation.get(),
            "volume": self.volume.get(),
            "logo_enabled": self.logo_enabled.get(),
            "logo_path": self.logo_path.get(),
            "logo_size": self.logo_size.get(),
            "low_resolution": self.low_resolution.get(),
            "export_quality": self.export_quality.get(),
        }

    def video_filter(self, settings):
        brightness = max(-1.0, min(1.0, settings["exposure"] / 2.0))
        filters = []
        if settings["auto_correct"]:
            filters.append("normalize=blackpt=black:whitept=white:smoothing=50")
        if settings["auto_white_balance"]:
            # Grey-edge is much less likely than gray-world to overcorrect night
            # footage when one colour (for example blue water) dominates a frame.
            filters.append("greyedge=difford=1:minknorm=5:sigma=2")
        black_out = max(0.0, settings["blacks"] * 0.08)
        black_in = max(0.0, -settings["blacks"] * 0.08)
        shadow_point = max(0.08, min(0.42, 0.25 + settings["shadows"] * 0.14))
        highlight_point = max(0.58, min(0.92, 0.75 + settings["highlights"] * 0.14))
        filters.append(f"curves=all='{black_in:.4f}/{black_out:.4f} 0.25/{shadow_point:.4f} 0.75/{highlight_point:.4f} 1/1'")
        if settings["whites"] >= 0:
            input_white = max(0.84, 1.0 - settings["whites"] * 0.12)
            filters.append(f"colorlevels=rimax={input_white:.4f}:gimax={input_white:.4f}:bimax={input_white:.4f}")
        else:
            output_white = max(0.84, 1.0 + settings["whites"] * 0.12)
            filters.append(f"colorlevels=romax={output_white:.4f}:gomax={output_white:.4f}:bomax={output_white:.4f}")
        colour_shift = settings["white_balance"] * 0.12 + settings["warmth"]
        red_gain = max(0.75, min(1.30, 1.0 + colour_shift))
        blue_gain = max(0.75, min(1.30, 1.0 - colour_shift))
        green_gain = max(0.94, 1.0 - abs(settings["white_balance"]) * 0.04)
        filters += [
            f"eq=brightness={brightness:.4f}:contrast={settings['contrast']:.4f}:saturation={settings['saturation']:.4f}",
            f"colorchannelmixer=rr={red_gain:.4f}:gg={green_gain:.4f}:bb={blue_gain:.4f}",
        ]
        return ",".join(filters)

    @staticmethod
    def quality_crf(quality, low_res=False):
        """Translate a staff-friendly 0–100 quality value to FFmpeg CRF."""
        quality = max(0.0, min(100.0, float(quality)))
        crf = round(36 - quality * 0.20)
        return min(40, crf + 4) if low_res else crf

    def preview_frame_command(self, source: Path, timestamp: float, target: Path, settings):
        base_filter = self.video_filter(settings) + ",scale=520:-2"
        if not settings["logo_enabled"]:
            return [ffmpeg_path(), "-y", "-ss", f"{timestamp:.3f}", "-i", str(source), "-frames:v", "1", "-vf", base_filter, str(target)]
        logo_fraction = int(settings["logo_size"].rstrip("%")) / 100.0
        logo_width = max(16, round(520 * logo_fraction))
        margin = 8
        opacity = 0.92
        shadow = 0.70
        graph = (
            f"[0:v]{base_filter}[base];[1:v]format=rgba,colorchannelmixer=aa={opacity:.3f}[rawlogo];"
            f"[rawlogo]scale=w={logo_width}:h=-1:force_original_aspect_ratio=decrease,setsar=1[logo];"
            f"[logo]split[mark][shadowin];[shadowin]colorchannelmixer=rr=0:gg=0:bb=0:aa={shadow:.3f},boxblur=5[shadow];"
            f"[base][shadow]overlay={margin + 3}:{margin + 3}[shadowed];[shadowed][mark]overlay={margin}:{margin}[outv]"
        )
        return [ffmpeg_path(), "-y", "-ss", f"{timestamp:.3f}", "-i", str(source), "-loop", "1", "-i", settings["logo_path"], "-filter_complex", graph, "-map", "[outv]", "-frames:v", "1", str(target)]

    def command(self, source: Path, target: Path, settings, low_res: bool = False):
        base_filter = self.video_filter(settings)
        if low_res:
            width, height = (int(v) for v in settings["low_resolution"].split("x"))
            base_filter += f",scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
        volume = f"volume={settings['volume']:.4f}"
        if settings["volume"] > 1.0:
            volume += ",alimiter=limit=0.95:attack=5:release=50"
        crf = str(self.quality_crf(settings["export_quality"], low_res))
        if not settings["logo_enabled"] and not low_res:
            return [ffmpeg_path(), "-y", "-i", str(source), "-vf", base_filter, "-af", volume, "-c:v", "libx264", "-preset", "fast", "-crf", crf, "-threads", "0", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(target)]
        frame_width = int(settings["low_resolution"].split("x")[0]) if low_res else self.video_dimensions(source)[0]
        logo_fraction = int(settings["logo_size"].rstrip("%")) / 100.0
        logo_width = max(16, round(frame_width * logo_fraction))
        margin = max(8, round(frame_width * 0.0125))
        opacity = 0.82 if low_res else 0.92
        shadow = 0.70
        graph = (
            f"[0:v]{base_filter}[base];[1:v]format=rgba,colorchannelmixer=aa={opacity:.3f}[rawlogo];"
            f"[rawlogo]scale=w={logo_width}:h=-1:force_original_aspect_ratio=decrease,setsar=1[logo];"
            f"[logo]split[mark][shadowin];[shadowin]colorchannelmixer=rr=0:gg=0:bb=0:aa={shadow:.3f},boxblur=8[shadow];"
            f"[base][shadow]overlay={margin + 5}:{margin + 5}[shadowed];[shadowed][mark]overlay={margin}:{margin}[outv]"
        )
        return [ffmpeg_path(), "-y", "-i", str(source), "-loop", "1", "-i", settings["logo_path"], "-filter_complex", graph, "-map", "[outv]", "-map", "0:a?", "-af", volume, "-c:v", "libx264", "-preset", "fast", "-crf", crf, "-threads", "0", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(target)]

    def low_res_from_completed_command(self, source: Path, target: Path, settings):
        """Resize an already corrected/logoed export without repeating expensive filters."""
        width, height = (int(v) for v in settings["low_resolution"].split("x"))
        resize = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
        crf = str(self.quality_crf(settings["export_quality"], True))
        return [ffmpeg_path(), "-y", "-i", str(source), "-vf", resize, "-c:v", "libx264", "-preset", "veryfast", "-crf", crf, "-threads", "0", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(target)]

    def process_batch(self, output: Path, low_output: Path | None, settings, selected, output_names):
        results = []
        completed_outputs = 0
        try:
            task_total = len(selected) * (2 if low_output is not None else 1)
            task_index = 0
            for index, clip in enumerate(selected):
                if self.stop_requested.is_set():
                    break
                target = output / output_names[index]
                duration = self.video_duration(clip.source)
                self.root.after(0, lambda c=clip: c.status.set("Starting FFmpeg…"))
                self.root.after(0, self.refresh_tree)
                return_code, full_log = self.run_export(self.command(clip.source, target, settings), duration, clip, "Full resolution", task_index, task_total)
                if self.stop_requested.is_set():
                    target.unlink(missing_ok=True)
                    self.root.after(0, lambda c=clip: c.status.set("Stopped"))
                    break
                task_index += 1
                ok = return_code == 0 and target.exists()
                if ok:
                    completed_outputs += 1
                    full_size = human_size(target.stat().st_size)
                    self.root.after(0, lambda c=clip, s=full_size: c.full_size.set(s))
                low_target = None
                low_error = ""
                if ok and low_output is not None:
                    low_target = low_output / output_names[index]
                    self.root.after(0, lambda c=clip: c.status.set("Starting low-res FFmpeg…"))
                    low_command = self.low_res_from_completed_command(target, low_target, settings) if settings["logo_enabled"] else self.command(clip.source, low_target, settings, low_res=True)
                    low_code, low_log = self.run_export(low_command, duration, clip, "Low resolution", task_index, task_total)
                    if self.stop_requested.is_set():
                        low_target.unlink(missing_ok=True)
                        self.root.after(0, lambda c=clip: c.status.set("Stopped"))
                        break
                    task_index += 1
                    low_ok = low_code == 0 and low_target.exists()
                    if low_ok:
                        completed_outputs += 1
                        low_size = human_size(low_target.stat().st_size)
                        self.root.after(0, lambda c=clip, s=low_size: c.low_size.set(s))
                    ok = ok and low_ok
                    if not low_ok:
                        low_error = low_log[-2000:]
                elif low_output is not None:
                    task_index += 1
                status = "Complete" if ok else "Failed"
                self.root.after(0, lambda c=clip, s=status: c.status.set(s))
                results.append({"source": str(clip.source), "output": str(target), "low_res_output": str(low_target) if low_target else None, "status": status, "error": "" if ok else (low_error or full_log[-2000:])})
                self.root.after(0, lambda p=task_index / task_total * 100: self.overall.configure(value=p))
                self.root.after(0, lambda done=task_index, total=task_total: self.summary.config(text=f"Creating videos: {done} of {total} outputs completed"))
                self.root.after(0, self.refresh_tree)
            (output / "TPS export summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
            if low_output is not None:
                (low_output / "TPS export summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
            completed = sum(r["status"] == "Complete" for r in results)
            if self.stop_requested.is_set():
                self.root.after(0, lambda: self._processing_stopped(completed_outputs, output, low_output))
                return
            folders = f"Full resolution:\n{output}"
            if low_output is not None:
                folders += f"\n\nLow resolution:\n{low_output}"
            self.root.after(0, lambda: self._processing_finished(completed, len(results), folders))
        except Exception as exc:
            self.root.after(0, lambda message=str(exc): self._processing_failed(message))

    def _processing_finished(self, completed, total, folders):
        self.processing = False
        self.export_started_at = None
        self.run_button.state(["!disabled"])
        self.stop_button.state(["disabled"])
        self.open_folder_button.state(["!disabled"])
        self.busy_notice.config(text="EXPORT COMPLETE", style="Card.TLabel")
        self.summary.config(text=f"Finished: {completed} of {total} videos created.")
        messagebox.showinfo(APP_NAME, f"Finished.\n\n{completed} of {total} videos were created.\n\n{folders}")

    def _processing_failed(self, message):
        self.processing = False
        self.export_started_at = None
        self.run_button.state(["!disabled"])
        self.stop_button.state(["disabled"])
        if self.last_output_folder and Path(self.last_output_folder).exists():
            self.open_folder_button.state(["!disabled"])
        self.busy_notice.config(text="EXPORT STOPPED — ERROR", style="Card.TLabel")
        self.summary.config(text="Video processing stopped—see the error message.")
        messagebox.showerror(APP_NAME, f"Video processing could not continue.\n\n{message}")

    def _processing_stopped(self, completed, output, low_output):
        self.processing = False
        self.export_started_at = None
        self.active_process = None
        self.run_button.state(["!disabled"])
        self.stop_button.state(["disabled"])
        self.open_folder_button.state(["!disabled"])
        self.busy_notice.config(text="EXPORT STOPPED BY USER", style="Card.TLabel")
        self.summary.config(text=f"Export stopped. {completed} completed video(s) were kept.")
        locations = f"Completed files kept in:\n{output}"
        if low_output is not None:
            locations += f"\n\nLow-resolution files:\n{low_output}"
        messagebox.showinfo(APP_NAME, f"Export stopped.\n\n{completed} completed video(s) were kept. The incomplete current file was removed.\n\n{locations}")


if __name__ == "__main__":
    if os.name == "nt":
        import ctypes
        _single_instance = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\TPSBulkVideoEditorSingleInstance")
        if ctypes.windll.kernel32.GetLastError() == 183:
            ctypes.windll.user32.MessageBoxW(None, "TPS Bulk Video Editor is already open.\n\nPlease use the existing window.", APP_NAME, 0x40)
            sys.exit(0)
    root = Tk()
    TPSVideoEditor(root)
    root.mainloop()

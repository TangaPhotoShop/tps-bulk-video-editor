from __future__ import annotations

import json
import statistics
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
from tkinter import BooleanVar, Canvas, DoubleVar, StringVar, TclError, Text, Tk, Toplevel, filedialog, messagebox, ttk

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont, ImageTk

APP_NAME = "TPS Bulk Video Editor"
APP_VERSION = "1.3.19"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi"}
DOLPHIN_NAMES = ("Echo", "Rani", "Calypso", "Comet", "Betts", "Zephyr", "Luna", "Storm", "Generic")
TRANSITION_SECONDS = 0.5


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
    current_resolution: StringVar
    quality_check: StringVar
    dolphin_name: StringVar
    add_intro: BooleanVar
    add_outro: BooleanVar


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
        self.destination = StringVar(value=r"Z:\7 DAY TOUR VIDEOS FOR LRC IMPORT")
        self.job_date = StringVar(value=datetime.now().strftime("%d-%b-%Y").upper())
        self.activity = StringVar(value="DOL")
        self.photographer = StringVar(value="")
        self.start_number = StringVar(value="0001")
        self.folder_name = StringVar(value=f"{self.job_date.get()}-DOL_[INITIALS]")
        self.first_filename = StringVar(value=f"{self.job_date.get()}-DOL-[INITIALS]-0001.MP4")
        self.low_res_enabled = BooleanVar(value=False)
        self.low_res_only = BooleanVar(value=False)
        self.low_res_copyright = BooleanVar(value=True)
        self.low_destination = StringVar(value=r"Z:\7 DAY LOW RES TOUR PHOTOS FOR UPLOAD")
        self.low_resolution = StringVar(value="640x480")
        self.batch_dolphin = StringVar(value="Generic")
        self.batch_add_intro = BooleanVar(value=False)
        self.batch_add_outro = BooleanVar(value=False)
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
        style.configure("Cancel.TButton", background="#c62828", foreground="white", font=("Segoe UI Semibold", 10), padding=(14, 9))
        style.map("Cancel.TButton", background=[("active", "#a91f1f")], foreground=[("active", "white")])
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
        right_outer = ttk.Frame(body, width=470)
        right_outer.pack(side="right", fill="y", padx=(8, 0))
        right_outer.pack_propagate(False)
        right_scroll = ttk.Scrollbar(right_outer, orient="vertical")
        right_scroll.pack(side="right", fill="y")
        right_canvas = Canvas(right_outer, background="#eef3f4", highlightthickness=0, yscrollcommand=right_scroll.set)
        right_canvas.pack(side="left", fill="both", expand=True)
        right_scroll.config(command=right_canvas.yview)
        right = ttk.Frame(right_canvas)
        right_window = right_canvas.create_window((0, 0), window=right, anchor="nw")
        right.bind("<Configure>", lambda _e: right_canvas.configure(scrollregion=right_canvas.bbox("all")))
        right_canvas.bind("<Configure>", lambda e: right_canvas.itemconfigure(right_window, width=e.width))

        def scroll_right(event):
            right_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

        right.bind("<Enter>", lambda _e: right_canvas.bind_all("<MouseWheel>", scroll_right))
        right.bind("<Leave>", lambda _e: right_canvas.unbind_all("<MouseWheel>"))
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))

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

        columns = ("item", "use", "source", "dolphin", "intro", "outro", "quality_check", "current_resolution", "original_size", "output", "full_resolution", "new_size", "low_resolution", "low_size", "status")
        tree_frame = ttk.Frame(files, style="Card.TFrame")
        tree_frame.pack(fill="both", expand=True)
        tree_scroll_y = ttk.Scrollbar(tree_frame, orient="vertical")
        tree_scroll_x = ttk.Scrollbar(tree_frame, orient="horizontal")
        self.tree = ttk.Treeview(
            tree_frame, columns=columns, show="headings", selectmode="extended", height=7,
            yscrollcommand=tree_scroll_y.set, xscrollcommand=tree_scroll_x.set,
        )
        tree_scroll_y.config(command=self.tree.yview)
        tree_scroll_x.config(command=self.tree.xview)
        self.tree.heading("item", text="Item")
        self.tree.heading("use", text="Use")
        self.tree.heading("source", text="Source file")
        self.tree.heading("dolphin", text="Dolphin")
        self.tree.heading("intro", text="Intro")
        self.tree.heading("outro", text="Outro")
        self.tree.heading("quality_check", text="Auto check")
        self.tree.heading("current_resolution", text="Current resolution")
        self.tree.heading("original_size", text="Original size")
        self.tree.heading("output", text="New filename")
        self.tree.heading("full_resolution", text="Full-res output")
        self.tree.heading("new_size", text="New size")
        self.tree.heading("low_resolution", text="Low-res output")
        self.tree.heading("low_size", text="Low-res size")
        self.tree.heading("status", text="Status")
        self.tree.column("item", width=45, anchor="center")
        self.tree.column("use", width=52, anchor="center")
        self.tree.column("source", width=220)
        self.tree.column("dolphin", width=85, anchor="center")
        self.tree.column("intro", width=55, anchor="center")
        self.tree.column("outro", width=55, anchor="center")
        self.tree.column("quality_check", width=145, anchor="center")
        self.tree.column("current_resolution", width=115, anchor="center")
        self.tree.column("original_size", width=90, anchor="e")
        self.tree.column("output", width=250)
        self.tree.column("full_resolution", width=105, anchor="center")
        self.tree.column("new_size", width=85, anchor="e")
        self.tree.column("low_resolution", width=100, anchor="center")
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

        sequence = ttk.LabelFrame(right, text="3. Optional dolphin intro and outro")
        sequence.pack(fill="x")
        sequence_checks = ttk.Frame(sequence, style="Card.TFrame")
        sequence_checks.pack(fill="x")
        ttk.Checkbutton(sequence_checks, text="Add intro", variable=self.batch_add_intro).pack(side="left")
        ttk.Checkbutton(sequence_checks, text="Add outro", variable=self.batch_add_outro).pack(side="left", padx=(12, 0))
        dolphin_row = ttk.Frame(sequence, style="Card.TFrame")
        dolphin_row.pack(fill="x", pady=(7, 0))
        ttk.Label(dolphin_row, text="Dolphin name", width=20, style="Card.TLabel").pack(side="left")
        ttk.Combobox(dolphin_row, textvariable=self.batch_dolphin, values=DOLPHIN_NAMES, state="readonly").pack(side="left", fill="x", expand=True)
        ttk.Button(sequence, text="APPLY TO SELECTED VIDEOS", command=self.apply_sequence_to_selected).pack(fill="x", pady=(7, 3))
        ttk.Label(sequence, text="Select one or more rows first, then apply the dolphin and intro/outro choices.", style="Card.TLabel", wraplength=350).pack(anchor="w")

        naming = ttk.LabelFrame(right, text="4. Rename and output")
        naming.pack(fill="x")
        name_parts = ttk.Frame(naming, style="Card.TFrame")
        name_parts.pack(fill="x", pady=3)
        ttk.Label(name_parts, text="Date", width=20, style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(name_parts, textvariable=self.job_date).grid(row=0, column=1, sticky="ew", pady=2)
        ttk.Label(name_parts, text="Photographer initials", width=20, style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(name_parts, textvariable=self.photographer).grid(row=1, column=1, sticky="ew", pady=2)
        ttk.Label(name_parts, text="Starting number", width=20, style="Card.TLabel").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(name_parts, textvariable=self.start_number).grid(row=2, column=1, sticky="ew", pady=2)
        name_parts.columnconfigure(1, weight=1)
        ttk.Label(naming, textvariable=self.first_filename, style="Card.TLabel").pack(anchor="w", pady=(0, 4))
        folder_row = ttk.Frame(naming, style="Card.TFrame")
        folder_row.pack(fill="x", pady=3)
        ttk.Label(folder_row, text="New folder name", width=19, style="Card.TLabel").pack(side="left")
        ttk.Label(folder_row, textvariable=self.folder_name, style="Card.TLabel", wraplength=210).pack(side="left", fill="x", expand=True)
        dest_row = ttk.Frame(naming, style="Card.TFrame")
        dest_row.pack(fill="x", pady=4)
        ttk.Combobox(dest_row, textvariable=self.destination, values=(r"Z:\7 DAY TOUR VIDEOS FOR LRC IMPORT",), state="normal").pack(side="left", fill="x", expand=True)
        ttk.Button(dest_row, text="Browse", command=self.choose_destination).pack(side="left", padx=(6, 0))
        low_toggle = ttk.Frame(naming, style="Card.TFrame")
        low_toggle.pack(fill="x", pady=(7, 2))
        ttk.Checkbutton(low_toggle, text="Also create low-res watermarked copies", variable=self.low_res_enabled, command=self.low_res_option_changed).pack(side="left")
        ttk.Combobox(low_toggle, textvariable=self.low_resolution, values=("640x480", "854x480", "1280x720"), state="readonly", width=10).pack(side="right")
        ttk.Checkbutton(naming, text="Create low-res watermarked videos only — skip full resolution", variable=self.low_res_only, command=self.low_res_option_changed).pack(anchor="w", pady=(1, 2))
        ttk.Checkbutton(naming, text="Diagonal copyright watermark on low-res videos (50% opacity)", variable=self.low_res_copyright).pack(anchor="w", pady=(1, 2))
        low_row = ttk.Frame(naming, style="Card.TFrame")
        low_row.pack(fill="x", pady=3)
        ttk.Combobox(low_row, textvariable=self.low_destination, values=(r"Z:\7 DAY LOW RES TOUR PHOTOS FOR UPLOAD",), state="normal").pack(side="left", fill="x", expand=True)
        ttk.Button(low_row, text="Browse", command=self.choose_low_destination).pack(side="left", padx=(6, 0))
        ttk.Label(naming, text="Folder: generated folder name + -LOW-RES", style="Card.TLabel").pack(anchor="w")
        quality_row = ttk.Frame(naming, style="Card.TFrame")
        quality_row.pack(fill="x", pady=(8, 2))
        ttk.Label(quality_row, text="Export quality", width=15, style="Card.TLabel").pack(side="left")
        quality_value = ttk.Label(quality_row, text="85%", width=5, style="Card.TLabel")
        quality_value.pack(side="right")
        ttk.Scale(quality_row, variable=self.export_quality, from_=0, to=100, command=lambda _v: quality_value.config(text=f"{self.export_quality.get():.0f}%")).pack(side="left", fill="x", expand=True)
        ttk.Label(naming, text="85% default • 70–100 recommended • lower = smaller file", style="Card.TLabel").pack(anchor="w")

        edits = ttk.LabelFrame(right, text="5. Bulk adjustments")
        edits.pack(fill="x", pady=10)
        self.edits_frame = edits
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

        logo = ttk.LabelFrame(right, text="6. TPS logo")
        logo.pack(fill="x")
        self.logo_frame = logo
        logo_controls = ttk.Frame(logo, style="Card.TFrame")
        logo_controls.pack(fill="x")
        ttk.Checkbutton(logo_controls, text="Add TPS logo — top left", variable=self.logo_enabled, command=self._adjustment_changed).pack(side="left")
        logo_picker = ttk.Combobox(logo_controls, textvariable=self.logo_size, values=("10%", "15%", "20%"), state="readonly", width=6)
        logo_picker.pack(side="right")
        logo_picker.bind("<<ComboboxSelected>>", lambda _e: self._adjustment_changed())
        ttk.Label(logo, text="Defaults to 15% • aspect ratio preserved • fixed drop shadow", style="Card.TLabel", wraplength=340).pack(anchor="w", pady=(4, 0))

        self.run_button = ttk.Button(right, text="CREATE EDITED VIDEOS", style="Primary.TButton", command=self.primary_export_action)
        self.run_button.pack(fill="x", pady=(12, 4))
        self.busy_notice = ttk.Label(right, text="READY TO EXPORT", anchor="center", style="Card.TLabel")
        self.busy_notice.pack(fill="x", pady=(2, 4))
        self.open_folder_button = ttk.Button(right, text="OPEN OUTPUT FOLDER", command=self.open_output_folder)
        self.open_folder_button.pack(fill="x", pady=(0, 4))
        self.open_folder_button.state(["disabled"])
        self.overall = ttk.Progressbar(right, maximum=100)
        self.overall.pack(fill="x")
        self.summary = ttk.Label(right, text="Original SD-card files are never changed.", wraplength=360)
        self.summary.pack(fill="x", pady=7)

        self.job_date.trace_add("write", lambda *_: self.update_generated_filename())
        self.photographer.trace_add("write", lambda *_: self.update_generated_filename())
        self.start_number.trace_add("write", lambda *_: self.update_generated_filename())
        self.low_resolution.trace_add("write", lambda *_: self.refresh_tree())
        self.update_generated_filename()

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
3. If required, choose a dolphin, tick Add intro and/or Add outro, then apply the choice to the selected rows.
4. Enter the date, photographer initials and starting number. The locked DOL filename is generated automatically.
5. Choose the destination where the new output folder will be created.
6. Select corrections, low-resolution copies and TPS logo settings as required.
7. Select Preview selected and move through the timeline to compare BEFORE and AFTER.
8. Select CREATE EDITED VIDEOS and watch the live progress display.

VIDEOS AND NAMING

Item — Numbers every source video 1, 2, 3 and so on, making it easy to count clips in a large folder.
Auto check — Samples the source in the background and shows OK, Dark, Colour/WB or both. After export, it checks the completed master again and changes the label to Export. A warning means review the clip in the preview; it does not automatically reject or alter it.
Current resolution — Detected from each source video in the background while you continue working.
Full-res output — Shows the source resolution that will be preserved, or Skipped when low-resolution-only mode is selected.
Low-res output — Shows the selected upload resolution, or Not selected when low-resolution copies are off.

OPTIONAL DOLPHIN INTRO AND OUTRO

Dolphin — Choose Echo, Rani, Calypso, Comet, Betts, Zephyr, Luna, Storm or Generic. Generic says the guest fed a wild dolphin without displaying a dolphin's name.
Add intro / Add outro — Either clip can be included independently. Both are OFF by default.
Apply to selected videos — Highlight one or more rows, choose the settings, then apply. If no rows are highlighted, the settings apply to every ticked video.
Transitions — The editor blends each join with a 0.5-second video crossfade and matching audio crossfade. Intro/outro graphics retain their prepared colours; bulk colour corrections apply only to the guest footage.

Date — Defaults to today's date. Enter as DD-MMM-YYYY, for example 15-SEP-2026.
Photographer initials — Enter the staff member's initials, for example SW. DOL is fixed automatically.
Starting number — Defaults to 0001. The generated filenames increase automatically: 0001, 0002, 0003 and so on.
Generated filename — Locked to DD-MMM-YYYY-DOL-INITIALS-0001.MP4 so staff cannot accidentally change the required pattern.
New folder name — Uses DD-MMM-YYYY-DOL_INITIALS, for example 15-SEP-2026-DOL_SW, and cannot conflict with an existing completed folder.
Destination — Defaults to Z:\\7 DAY TOUR VIDEOS FOR LRC IMPORT. Select it from the editable list, type an override or use Browse if that drive/folder is unavailable.

LOW-RESOLUTION COPIES

Also create low-res watermarked copies — Creates a second upload-ready batch with the same filenames.
Create low-res watermarked videos only — Creates only the upload-ready watermarked batch and skips the full-resolution exports.
Diagonal copyright watermark — Adds “Copyright Tangalooma Photo Shop” diagonally across low-resolution videos at 50% opacity. Default: ON. It never appears on the full-resolution master.
Resolution — 640x480, 854x480 or 1280x720. The image is never stretched; padding is added when required.
Low-res destination — Defaults to Z:\\7 DAY LOW RES TOUR PHOTOS FOR UPLOAD. Select it from the editable list, type an override or use Browse if unavailable. Its generated folder name ends with -LOW-RES.
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

The orange BUSY notice shows which video is processing, for example 1 of 10, plus the percentage and ETA for the entire batch. The status column shows progress within the current full- or low-resolution output. While processing, the main button becomes a red CANCEL BATCH EXPORT button that remains visible. It asks for confirmation, keeps completed videos and removes the incomplete file currently being written. Original SD-card files are never modified or deleted.

STARTUP DEFAULTS — VERSION {APP_VERSION}

Intro OFF | Outro OFF | Dolphin Generic | Auto Exposure OFF | Auto White Balance OFF | Exposure 0.00 | Contrast 1.00 | Shadows 0.00 | Highlights 0.00 | Blacks 0.00 | Whites 0.00 | White Balance 0.00 | Warmth 0.00 | Saturation 1.00 | Volume 1.00 | Export quality 85% | Low-resolution copies OFF | Low-resolution only OFF | Low-res copyright ON | TPS logo ON | Logo size 15%
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
        self.clips = [
            Clip(
                p, BooleanVar(value=True), StringVar(value="Ready"), DoubleVar(value=0),
                StringVar(value="—"), StringVar(value="—"), StringVar(value="Reading…"),
                StringVar(value="Checking…"), StringVar(value=self.batch_dolphin.get()),
                BooleanVar(value=self.batch_add_intro.get()), BooleanVar(value=self.batch_add_outro.get()),
            )
            for p in paths
        ]
        names = [p.name for p in paths]
        self.preview_chooser["values"] = names
        self.preview_selected_name.set(names[0] if names else "")
        self.refresh_tree()
        if names:
            self._schedule_preview(immediate=True)
            threading.Thread(target=self.probe_resolutions, args=(list(self.clips),), daemon=True).start()

    def probe_resolutions(self, clips):
        for clip in clips:
            try:
                width, height = self.video_dimensions(clip.source)
                resolution = f"{width}×{height}"
            except Exception:
                resolution = "Unavailable"
            quality = self.video_quality_check(clip.source)
            self.root.after(0, self.set_clip_media_info, clip, resolution, quality)

    def set_clip_media_info(self, clip, resolution, quality):
        # Ignore results from an older scan if staff have already opened another folder.
        if clip in self.clips:
            clip.current_resolution.set(resolution)
            clip.quality_check.set(quality)
            self.refresh_tree()

    def video_quality_check(self, source: Path) -> str:
        """Sparsely sample luminance/chroma and return a conservative review flag."""
        command = [
            ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-i", str(source),
            "-vf", "fps=1/5,scale=160:-2,signalstats,metadata=print:file=-",
            "-frames:v", "30", "-f", "null", "-",
        ]
        try:
            proc = self.run_hidden(command, capture_output=True, text=True)
            report = (proc.stdout or "") + "\n" + (proc.stderr or "")
            y_values = [float(value) for value in re.findall(r"lavfi\.signalstats\.YAVG=([\d.]+)", report)]
            u_values = [float(value) for value in re.findall(r"lavfi\.signalstats\.UAVG=([\d.]+)", report)]
            v_values = [float(value) for value in re.findall(r"lavfi\.signalstats\.VAVG=([\d.]+)", report)]
            if not y_values:
                return "Check unavailable"
            warnings = []
            median_y = statistics.median(y_values)
            dark_share = sum(value < 42 for value in y_values) / len(y_values)
            if median_y < 52 or dark_share >= 0.5:
                warnings.append("Dark")
            if u_values and v_values:
                median_u = statistics.median(u_values)
                median_v = statistics.median(v_values)
                chroma_cast = ((median_u - 128) ** 2 + (median_v - 128) ** 2) ** 0.5
                if chroma_cast > 15:
                    warnings.append("Colour/WB")
            return "⚠ " + " + ".join(warnings) if warnings else "✓ OK"
        except Exception:
            return "Check unavailable"

    def filename_parts(self):
        date_text = self.job_date.get().strip().upper()
        initials = clean_code(self.photographer.get(), "[INITIALS]")
        try:
            number = max(1, int(self.start_number.get()))
        except (ValueError, TclError):
            number = 1
        return f"{date_text}-DOL-{initials}", number, 4

    def update_generated_filename(self):
        prefix, number, width = self.filename_parts()
        self.first_filename.set(f"{prefix}-{number:0{width}d}.MP4")
        date_text = self.job_date.get().strip().upper()
        initials = clean_code(self.photographer.get(), "[INITIALS]")
        self.folder_name.set(f"{date_text}-DOL_{initials}")
        if hasattr(self, "tree"):
            self.refresh_tree()

    def low_res_option_changed(self):
        if self.low_res_only.get():
            self.low_res_enabled.set(True)
            self.logo_enabled.set(True)
        elif not self.low_res_enabled.get():
            self.low_res_only.set(False)
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
            if clip.selected.get():
                full_resolution = "Skipped" if self.low_res_only.get() else clip.current_resolution.get()
                low_resolution = self.low_resolution.get().replace("x", "×") if self.low_res_enabled.get() else "Not selected"
            else:
                full_resolution = "—"
                low_resolution = "—"
            self.tree.insert("", "end", iid=str(index), values=(
                index + 1, "✓" if clip.selected.get() else "", clip.source.name,
                clip.dolphin_name.get(), "✓" if clip.add_intro.get() else "—", "✓" if clip.add_outro.get() else "—",
                clip.quality_check.get(), clip.current_resolution.get(), original_size, name, full_resolution,
                clip.full_size.get(), low_resolution, clip.low_size.get(), clip.status.get(),
            ))
        self.count_label.config(text=f"{selected_index} of {len(self.clips)} videos selected")

    def apply_sequence_to_selected(self):
        rows = self.tree.selection()
        targets = [self.clips[int(row)] for row in rows] if rows else self.selected_clips()
        if not targets:
            return messagebox.showinfo(APP_NAME, "Select at least one video first.")
        for clip in targets:
            clip.dolphin_name.set(self.batch_dolphin.get())
            clip.add_intro.set(self.batch_add_intro.get())
            clip.add_outro.set(self.batch_add_outro.get())
        self.refresh_tree()
        self.summary.config(text=f"Intro/outro settings applied to {len(targets)} video{'s' if len(targets) != 1 else ''}.")

    def toggle_row(self, event):
        if self.tree.identify_region(event.x, event.y) == "cell" and self.tree.identify_column(event.x) == "#2":
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

    @staticmethod
    def intro_outro_asset(dolphin_name: str, kind: str) -> Path:
        stem = "Wild-Dolphin" if dolphin_name == "Generic" else dolphin_name
        return Path(__file__).with_name("assets") / "intro-outro" / f"{stem}-{kind}-1080p.mp4"

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

    def run_export(self, command, duration: float, clip: Clip, stage: str, task_index: int, task_total: int, video_index: int, video_total: int):
        progress_command = command[:-1] + ["-progress", "pipe:1", "-nostats", command[-1]]
        proc = self.popen_hidden(progress_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        self.active_process = proc
        self.root.after(0, lambda i=video_index, total=video_total, s=stage: self.busy_notice.config(text=f"BUSY — PROCESSING {i} OF {total} — 0%\n{s}", style="Busy.TLabel"))
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
                    eta_short = f"ETA {int(remaining // 60)}m {int(remaining % 60)}s" if overall_percent >= 1 else "Calculating ETA…"
                    self.root.after(0, lambda p=overall_percent: self.overall.configure(value=p))
                    self.root.after(0, lambda v=size_var, s=current_size: v.set(s))
                    self.root.after(0, lambda c=clip, s=stage, p=current_percent: c.status.set(f"{s} {p:.0f}%"))
                    self.root.after(0, lambda i=video_index, total=video_total, p=overall_percent, s=stage, eta=eta_short: self.busy_notice.config(text=f"BUSY — PROCESSING {i} OF {total} — {p:.0f}%\n{s} • {eta}", style="Busy.TLabel"))
                    self.root.after(0, lambda p=overall_percent, eta=eta_short: self.summary.config(text=f"Overall batch: {p:.0f}% • {eta}"))
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
        self.run_button.state(["disabled"])
        self.busy_notice.config(text="STOPPING EXPORT SAFELY…", style="Busy.TLabel")
        self.summary.config(text="Stopping after the current FFmpeg process closes…")
        proc = self.active_process
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def primary_export_action(self):
        if self.processing:
            self.request_stop()
        else:
            self.start_processing()

    def set_processing_controls(self, active: bool):
        if active:
            self.edits_frame.pack_forget()
            self.logo_frame.pack_forget()
            self.run_button.config(text="CANCEL BATCH EXPORT", style="Cancel.TButton")
        else:
            self.edits_frame.pack(fill="x", pady=10, before=self.run_button)
            self.logo_frame.pack(fill="x", before=self.run_button)
            self.run_button.config(text="CREATE EDITED VIDEOS", style="Primary.TButton")
        self.run_button.state(["!disabled"])

    def open_output_folder(self):
        if self.last_output_folder and Path(self.last_output_folder).exists():
            if os.name == "nt":
                os.startfile(str(self.last_output_folder))
            else:
                subprocess.Popen(["xdg-open", str(self.last_output_folder)])

    def validate(self):
        if not self.selected_clips():
            return "Select at least one video."
        try:
            datetime.strptime(self.job_date.get().strip().upper(), "%d-%b-%Y")
        except ValueError:
            return "Enter the date as DD-MMM-YYYY, for example 15-SEP-2026."
        if not re.fullmatch(r"[A-Za-z]{1,6}", self.photographer.get().strip()):
            return "Enter the photographer initials using 1 to 6 letters only."
        try:
            if int(self.start_number.get()) < 1:
                return "Starting number must be 1 or higher."
        except (ValueError, TclError):
            return "Enter a valid starting number."
        if not self.low_res_only.get() and not self.destination.get():
            return "Choose an output destination."
        if (self.low_res_enabled.get() or self.low_res_only.get()) and not (self.low_destination.get() or self.destination.get()):
            return "Choose a destination for the low-resolution copies."
        if (self.logo_enabled.get() or self.low_res_enabled.get() or self.low_res_only.get()) and not Path(self.logo_path.get()).exists():
            return "Choose the TPS logo PNG, or turn the logo off."
        for clip in self.selected_clips():
            if clip.dolphin_name.get() not in DOLPHIN_NAMES:
                return f"Choose a valid dolphin for {clip.source.name}."
            for enabled, kind in ((clip.add_intro.get(), "Intro"), (clip.add_outro.get(), "Outro")):
                if enabled and not self.intro_outro_asset(clip.dolphin_name.get(), kind).exists():
                    return f"The {kind.lower()} video for {clip.dolphin_name.get()} is missing. Reinstall the editor before exporting."
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
            low_only = self.low_res_only.get()
            destination_parent = Path((self.low_destination.get() or self.destination.get()) if low_only else self.destination.get())
            destination_parent.mkdir(parents=True, exist_ok=True)
            free_space = shutil.disk_usage(destination_parent).free
            selected_bytes = sum(c.source.stat().st_size for c in self.selected_clips())
            required = selected_bytes * (0.35 if low_only else (1.25 if self.low_res_enabled.get() else 1.1))
            if free_space < required:
                return messagebox.showerror(APP_NAME, f"There may not be enough free space at the destination.\n\nEstimated requirement: {human_size(int(required))}\nAvailable: {human_size(free_space)}")
            output = None if low_only else self.create_unique_folder(Path(self.destination.get()), clean_code(self.folder_name.get(), "TPS-Edited-Videos"))
            low_output = None
            if self.low_res_enabled.get() or low_only:
                low_parent = Path(self.low_destination.get() or self.destination.get())
                low_output = self.create_unique_folder(low_parent, f"{clean_code(self.folder_name.get(), 'TPS-Edited-Videos')}-LOW-RES")
        except OSError as exc:
            self.summary.config(text="Could not create the output folder.")
            return messagebox.showerror(APP_NAME, f"The output folder could not be created.\n\n{exc}")
        self.processing = True
        self.export_started_at = time.monotonic()
        self.last_output_folder = low_output if low_only else output
        self.open_folder_button.state(["disabled"])
        self.stop_requested.clear()
        self.set_processing_controls(True)
        self.busy_notice.config(text="BUSY — PREPARING EXPORT…", style="Busy.TLabel")
        self.summary.config(text="Starting video processing…")
        self.overall.configure(value=0)
        settings = self.settings_snapshot()
        settings["low_res_only"] = low_only
        selected = self.selected_clips()
        settings["clip_sequences"] = [
            {"dolphin": clip.dolphin_name.get(), "add_intro": clip.add_intro.get(), "add_outro": clip.add_outro.get()}
            for clip in selected
        ]
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
            "low_res_copyright": self.low_res_copyright.get(),
            "export_quality": self.export_quality.get(),
        }

    @staticmethod
    def copyright_watermark(width: int, height: int) -> Path:
        """Create a reusable transparent, diagonal copyright overlay."""
        target = Path(tempfile.gettempdir()) / f"tps-copyright-{width}x{height}.png"
        if target.exists():
            return target
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        font_size = max(18, round(width * 0.045))
        font = None
        for candidate in (r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\segoeuib.ttf", "DejaVuSans-Bold.ttf"):
            try:
                font = ImageFont.truetype(candidate, font_size)
                break
            except OSError:
                continue
        if font is None:
            font = ImageFont.load_default()
        text = "Copyright Tangalooma Photo Shop"
        probe = ImageDraw.Draw(canvas)
        box = probe.textbbox((0, 0), text, font=font, stroke_width=2)
        text_layer = Image.new("RGBA", (box[2] - box[0] + 30, box[3] - box[1] + 30), (0, 0, 0, 0))
        draw = ImageDraw.Draw(text_layer)
        draw.text((15 - box[0], 15 - box[1]), text, font=font, fill=(255, 255, 255, 128), stroke_width=2, stroke_fill=(0, 0, 0, 96))
        rotated = text_layer.rotate(25, expand=True, resample=Image.Resampling.BICUBIC)
        canvas.alpha_composite(rotated, ((width - rotated.width) // 2, (height - rotated.height) // 2))
        canvas.save(target)
        return target

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
        width = height = 0
        if low_res:
            width, height = (int(v) for v in settings["low_resolution"].split("x"))
            base_filter += f",scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
        volume = f"volume={settings['volume']:.4f}"
        if settings["volume"] > 1.0:
            volume += ",alimiter=limit=0.95:attack=5:release=50"
        crf = str(self.quality_crf(settings["export_quality"], low_res))
        use_logo = settings["logo_enabled"]
        use_copyright = low_res and settings.get("low_res_copyright", True)
        if not use_logo and not use_copyright:
            return [ffmpeg_path(), "-y", "-i", str(source), "-vf", base_filter, "-af", volume, "-c:v", "libx264", "-preset", "fast", "-crf", crf, "-threads", "0", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(target)]
        inputs = [ffmpeg_path(), "-y", "-i", str(source)]
        graph = [f"[0:v]{base_filter}[base]"]
        current = "base"
        input_index = 1
        if use_logo:
            inputs += ["-loop", "1", "-i", settings["logo_path"]]
            frame_width = width if low_res else self.video_dimensions(source)[0]
            logo_width = max(16, round(frame_width * int(settings["logo_size"].rstrip("%")) / 100.0))
            margin = max(8, round(frame_width * 0.0125))
            opacity = 0.82 if low_res else 0.92
            graph += [
                f"[{input_index}:v]format=rgba,colorchannelmixer=aa={opacity:.3f},scale=w={logo_width}:h=-1:force_original_aspect_ratio=decrease,setsar=1[logo]",
                "[logo]split[mark][shadowin]",
                "[shadowin]colorchannelmixer=rr=0:gg=0:bb=0:aa=0.700,boxblur=8[shadow]",
                f"[{current}][shadow]overlay={margin + 5}:{margin + 5}[shadowed]",
                f"[shadowed][mark]overlay={margin}:{margin}[branded]",
            ]
            current = "branded"
            input_index += 1
        if use_copyright:
            inputs += ["-loop", "1", "-i", str(self.copyright_watermark(width, height))]
            graph.append(f"[{current}][{input_index}:v]overlay=0:0[outv]")
        else:
            graph.append(f"[{current}]null[outv]")
        return inputs + ["-filter_complex", ";".join(graph), "-map", "[outv]", "-map", "0:a?", "-af", volume, "-c:v", "libx264", "-preset", "fast", "-crf", crf, "-threads", "0", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(target)]

    def low_res_from_completed_command(self, source: Path, target: Path, settings):
        """Resize an already corrected/logoed export without repeating expensive filters."""
        width, height = (int(v) for v in settings["low_resolution"].split("x"))
        resize = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
        crf = str(self.quality_crf(settings["export_quality"], True))
        if settings.get("low_res_copyright", True):
            graph = f"[0:v]{resize}[base];[base][1:v]overlay=0:0[outv]"
            return [ffmpeg_path(), "-y", "-i", str(source), "-loop", "1", "-i", str(self.copyright_watermark(width, height)), "-filter_complex", graph, "-map", "[outv]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", crf, "-threads", "0", "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(target)]
        return [ffmpeg_path(), "-y", "-i", str(source), "-vf", resize, "-c:v", "libx264", "-preset", "veryfast", "-crf", crf, "-threads", "0", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(target)]

    def intro_outro_command(self, guest: Path, target: Path, sequence, guest_duration: float, settings):
        """Join the untouched branded intro/outro to an already edited guest master."""
        add_intro = sequence["add_intro"]
        add_outro = sequence["add_outro"]
        width, height = self.video_dimensions(guest)
        common_video = (
            f"scale={width}:{height},format=yuv420p,"
            f"fps=30,settb=AVTB,setpts=PTS-STARTPTS"
        )
        inputs = [ffmpeg_path(), "-y", "-i", str(guest)]
        graph = [f"[0:v]{common_video}[guestv]", "[0:a]aresample=48000,asetpts=PTS-STARTPTS[guesta]"]
        next_input = 1
        if add_intro:
            inputs += ["-i", str(self.intro_outro_asset(sequence["dolphin"], "Intro"))]
            graph += [
                f"[{next_input}:v]{common_video}[introv]",
                f"[{next_input}:a]aresample=48000,loudnorm=I=-16:TP=-2:LRA=7,asetpts=PTS-STARTPTS[introa]",
            ]
            intro_index = next_input
            next_input += 1
        if add_outro:
            inputs += ["-i", str(self.intro_outro_asset(sequence["dolphin"], "Outro"))]
            graph += [
                f"[{next_input}:v]{common_video}[outrov]",
                f"[{next_input}:a]aresample=48000,loudnorm=I=-16:TP=-2:LRA=7,asetpts=PTS-STARTPTS[outroa]",
            ]
        fade = TRANSITION_SECONDS
        blend = f"blend=all_expr='A*(1-T/{fade})+B*(T/{fade})':shortest=1"
        if add_intro and add_outro:
            graph += [
                "[introv]split=2[introprein][introfadein]",
                "[guestv]split=3[guestheadin][guestmidin][guesttailin]",
                "[outrov]split=2[outrofadein][outropostin]",
                f"[introprein]trim=start=0:end={6.0-fade},setpts=PTS-STARTPTS[intropre]",
                f"[introfadein]trim=start={6.0-fade}:end=6,setpts=PTS-STARTPTS[introfade]",
                f"[guestheadin]trim=start=0:end={fade},setpts=PTS-STARTPTS[guesthead]",
                f"[introfade][guesthead]{blend}[blendin]",
                f"[guestmidin]trim=start={fade}:end={max(fade, guest_duration-fade)},setpts=PTS-STARTPTS[guestmid]",
                f"[guesttailin]trim=start={max(0.0, guest_duration-fade)}:end={guest_duration},setpts=PTS-STARTPTS[guesttail]",
                f"[outrofadein]trim=start=0:end={fade},setpts=PTS-STARTPTS[outrofade]",
                f"[guesttail][outrofade]{blend}[blendout]",
                f"[outropostin]trim=start={fade}:end=6,setpts=PTS-STARTPTS[outropost]",
                "[intropre][blendin][guestmid][blendout][outropost]concat=n=5:v=1:a=0[outv]",
                f"[introa][guesta]acrossfade=d={fade}:c1=tri:c2=tri[joineda]",
                f"[joineda][outroa]acrossfade=d={fade}:c1=tri:c2=tri[outa]",
            ]
        elif add_intro:
            graph += [
                "[introv]split=2[introprein][introfadein]", "[guestv]split=2[guestheadin][guestpostin]",
                f"[introprein]trim=start=0:end={6.0-fade},setpts=PTS-STARTPTS[intropre]",
                f"[introfadein]trim=start={6.0-fade}:end=6,setpts=PTS-STARTPTS[introfade]",
                f"[guestheadin]trim=start=0:end={fade},setpts=PTS-STARTPTS[guesthead]",
                f"[introfade][guesthead]{blend}[blendin]",
                f"[guestpostin]trim=start={fade}:end={guest_duration},setpts=PTS-STARTPTS[guestpost]",
                "[intropre][blendin][guestpost]concat=n=3:v=1:a=0[outv]",
                f"[introa][guesta]acrossfade=d={fade}:c1=tri:c2=tri[outa]",
            ]
        else:
            graph += [
                "[guestv]split=2[guestprein][guestfadein]", "[outrov]split=2[outrofadein][outropostin]",
                f"[guestprein]trim=start=0:end={max(0.0, guest_duration-fade)},setpts=PTS-STARTPTS[guestpre]",
                f"[guestfadein]trim=start={max(0.0, guest_duration-fade)}:end={guest_duration},setpts=PTS-STARTPTS[guestfade]",
                f"[outrofadein]trim=start=0:end={fade},setpts=PTS-STARTPTS[outrofade]",
                f"[guestfade][outrofade]{blend}[blendout]",
                f"[outropostin]trim=start={fade}:end=6,setpts=PTS-STARTPTS[outropost]",
                "[guestpre][blendout][outropost]concat=n=3:v=1:a=0[outv]",
                f"[guesta][outroa]acrossfade=d={fade}:c1=tri:c2=tri[outa]",
            ]
        crf = str(self.quality_crf(settings["export_quality"], False))
        return inputs + [
            "-filter_complex", ";".join(graph), "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-preset", "fast", "-crf", crf, "-threads", "0",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(target),
        ]

    @staticmethod
    def final_duration(guest_duration: float, sequence) -> float:
        additions = int(sequence["add_intro"]) + int(sequence["add_outro"])
        return guest_duration + additions * (6.0 - TRANSITION_SECONDS)

    def process_batch(self, output: Path | None, low_output: Path | None, settings, selected, output_names):
        results = []
        completed_outputs = 0
        try:
            low_only = settings.get("low_res_only", False)
            sequences = settings["clip_sequences"]
            task_total = sum(
                (3 if low_only and (sequence["add_intro"] or sequence["add_outro"]) else 1)
                if low_only else
                (1 + int(sequence["add_intro"] or sequence["add_outro"]) + int(low_output is not None))
                for sequence in sequences
            )
            task_index = 0
            for index, clip in enumerate(selected):
                if self.stop_requested.is_set():
                    break
                duration = self.video_duration(clip.source)
                sequence = sequences[index]
                use_sequence = sequence["add_intro"] or sequence["add_outro"]
                target = None
                low_target = None
                full_log = ""
                low_error = ""
                ok = True
                work_parent = low_output if low_only else output
                guest_work = work_parent / f".{Path(output_names[index]).stem}.guest-working.mp4"
                joined_work = work_parent / f".{Path(output_names[index]).stem}.joined-working.mp4"
                try:
                    if low_only and not use_sequence:
                        low_target = low_output / output_names[index]
                        code, full_log = self.run_export(self.command(clip.source, low_target, settings, low_res=True), duration, clip, "Low resolution", task_index, task_total, index + 1, len(selected))
                        task_index += 1
                        ok = code == 0 and low_target.exists()
                    else:
                        target = (output / output_names[index]) if not low_only else joined_work
                        first_target = guest_work if use_sequence else target
                        code, full_log = self.run_export(self.command(clip.source, first_target, settings), duration, clip, "Editing guest video", task_index, task_total, index + 1, len(selected))
                        task_index += 1
                        ok = code == 0 and first_target.exists()
                        if ok and use_sequence:
                            final_master = target
                            joined_duration = self.final_duration(duration, sequence)
                            code, join_log = self.run_export(self.intro_outro_command(first_target, final_master, sequence, duration, settings), joined_duration, clip, f"Adding {sequence['dolphin']} intro/outro", task_index, task_total, index + 1, len(selected))
                            task_index += 1
                            full_log += "\n" + join_log
                            ok = code == 0 and final_master.exists()
                        if ok and not low_only:
                            completed_outputs += 1
                            full_size = human_size(target.stat().st_size)
                            self.root.after(0, lambda c=clip, s=full_size: c.full_size.set(s))
                        if ok and low_output is not None:
                            low_target = low_output / output_names[index]
                            master = target
                            master_duration = self.final_duration(duration, sequence) if use_sequence else duration
                            low_code, low_log = self.run_export(self.low_res_from_completed_command(master, low_target, settings), master_duration, clip, "Low resolution", task_index, task_total, index + 1, len(selected))
                            task_index += 1
                            ok = low_code == 0 and low_target.exists()
                            if not ok:
                                low_error = low_log[-2000:]
                    if self.stop_requested.is_set():
                        if low_target:
                            low_target.unlink(missing_ok=True)
                        elif target:
                            target.unlink(missing_ok=True)
                        self.root.after(0, lambda c=clip: c.status.set("Stopped"))
                        break
                    if ok and low_target is not None:
                        completed_outputs += 1
                        low_size = human_size(low_target.stat().st_size)
                        self.root.after(0, lambda c=clip, s=low_size: c.low_size.set(s))
                    if ok:
                        check_target = low_target if low_only else target
                        self.root.after(0, lambda i=index + 1, total=len(selected): self.busy_notice.config(text=f"BUSY — QUALITY CHECK {i} OF {total}", style="Busy.TLabel"))
                        export_check = self.video_quality_check(check_target)
                        self.root.after(0, lambda c=clip, q=export_check: c.quality_check.set(f"Export: {q}"))
                finally:
                    guest_work.unlink(missing_ok=True)
                    joined_work.unlink(missing_ok=True)
                status = "Complete" if ok else "Failed"
                self.root.after(0, lambda c=clip, s=status: c.status.set(s))
                results.append({"source": str(clip.source), "dolphin": sequence["dolphin"], "intro": sequence["add_intro"], "outro": sequence["add_outro"], "output": str(target) if target and not low_only else None, "low_res_output": str(low_target) if low_target else None, "status": status, "error": "" if ok else (low_error or full_log[-2000:])})
                self.root.after(0, lambda p=task_index / task_total * 100: self.overall.configure(value=p))
                self.root.after(0, lambda done=task_index, total=task_total: self.summary.config(text=f"Creating videos: {done} of {total} outputs completed"))
                self.root.after(0, self.refresh_tree)
            if output is not None:
                (output / "TPS export summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
            if low_output is not None:
                (low_output / "TPS export summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
            completed = sum(r["status"] == "Complete" for r in results)
            if self.stop_requested.is_set():
                self.root.after(0, lambda: self._processing_stopped(completed_outputs, output, low_output))
                return
            folders = f"Full resolution:\n{output}" if output is not None else ""
            if low_output is not None:
                folders += ("\n\n" if folders else "") + f"Low resolution:\n{low_output}"
            self.root.after(0, lambda: self._processing_finished(completed, len(results), folders))
        except Exception as exc:
            self.root.after(0, lambda message=str(exc): self._processing_failed(message))

    def _processing_finished(self, completed, total, folders):
        self.processing = False
        self.export_started_at = None
        self.set_processing_controls(False)
        self.open_folder_button.state(["!disabled"])
        self.busy_notice.config(text="EXPORT COMPLETE", style="Card.TLabel")
        self.summary.config(text=f"Finished: {completed} of {total} videos created.")
        messagebox.showinfo(APP_NAME, f"Finished.\n\n{completed} of {total} videos were created.\n\n{folders}")

    def _processing_failed(self, message):
        self.processing = False
        self.export_started_at = None
        self.set_processing_controls(False)
        if self.last_output_folder and Path(self.last_output_folder).exists():
            self.open_folder_button.state(["!disabled"])
        self.busy_notice.config(text="EXPORT STOPPED — ERROR", style="Card.TLabel")
        self.summary.config(text="Video processing stopped—see the error message.")
        messagebox.showerror(APP_NAME, f"Video processing could not continue.\n\n{message}")

    def _processing_stopped(self, completed, output, low_output):
        self.processing = False
        self.export_started_at = None
        self.active_process = None
        self.set_processing_controls(False)
        self.open_folder_button.state(["!disabled"])
        self.busy_notice.config(text="EXPORT STOPPED BY USER", style="Card.TLabel")
        self.summary.config(text=f"Export stopped. {completed} completed video(s) were kept.")
        locations = f"Completed full-resolution files kept in:\n{output}" if output is not None else ""
        if low_output is not None:
            locations += ("\n\n" if locations else "") + f"Completed low-resolution files kept in:\n{low_output}"
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

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import BooleanVar, DoubleVar, IntVar, StringVar, Tk, Toplevel, filedialog, messagebox, ttk

import imageio_ffmpeg
from PIL import Image, ImageTk

APP_NAME = "TPS Bulk Video Editor"
APP_VERSION = "1.3.3"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi"}


def clean_code(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", value.strip()).strip("-").upper()
    return cleaned or fallback


def ffmpeg_path() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


@dataclass
class Clip:
    source: Path
    selected: BooleanVar
    status: StringVar
    progress: DoubleVar


class TPSVideoEditor:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title(f"{APP_NAME} — Version {APP_VERSION}")
        self.root.geometry("1280x820")
        self.root.minsize(1000, 680)
        self.root.configure(bg="#eef3f4")
        self.clips: list[Clip] = []
        self.preview_image = None
        self.processing = False

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
        self.auto_correct = BooleanVar(value=False)
        self.auto_white_balance = BooleanVar(value=False)
        self.exposure = DoubleVar(value=0.0)
        self.contrast = DoubleVar(value=1.0)
        self.warmth = DoubleVar(value=0.0)
        self.red_balance = DoubleVar(value=1.0)
        self.blue_balance = DoubleVar(value=1.0)
        self.volume = DoubleVar(value=1.0)
        self.logo_enabled = BooleanVar(value=True)
        self.logo_path = StringVar(value=str(Path(__file__).with_name("assets") / "tps-logo.png"))

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
        style.configure("TButton", padding=(10, 7))
        style.configure("TLabelframe", background="white", padding=12)
        style.configure("TLabelframe.Label", background="white", foreground="#073f49", font=("Segoe UI Semibold", 11))

    def _build(self):
        header = ttk.Frame(self.root, padding=(20, 14), style="Card.TFrame")
        header.pack(fill="x")
        ttk.Label(header, text=f"TPS BULK VIDEO EDITOR  •  VERSION {APP_VERSION}", style="Title.TLabel", padding=(14, 8)).pack(side="left")
        ttk.Label(header, text="SD card → adjust → rename → new output folder", style="Card.TLabel").pack(side="left", padx=18)

        body = ttk.Frame(self.root, padding=16)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        right = ttk.Frame(body, width=390)
        right.pack(side="right", fill="y", padx=(8, 0))

        files = ttk.LabelFrame(left, text="1. Videos from SD card")
        files.pack(fill="both", expand=True)
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

        columns = ("use", "source", "output", "status")
        self.tree = ttk.Treeview(files, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("use", text="Use")
        self.tree.heading("source", text="Source file")
        self.tree.heading("output", text="New filename")
        self.tree.heading("status", text="Status")
        self.tree.column("use", width=52, anchor="center")
        self.tree.column("source", width=260)
        self.tree.column("output", width=285)
        self.tree.column("status", width=120)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Button-1>", self.toggle_row)

        naming = ttk.LabelFrame(right, text="2. Rename and output")
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

        edits = ttk.LabelFrame(right, text="3. Bulk adjustments")
        edits.pack(fill="x", pady=10)
        self._slider(edits, "Exposure", self.exposure, -1.5, 1.5)
        self._slider(edits, "Contrast", self.contrast, 0.7, 1.4)
        self._slider(edits, "Warmth", self.warmth, -0.3, 0.3)
        self._slider(edits, "Red balance", self.red_balance, 0.8, 1.2)
        self._slider(edits, "Blue balance", self.blue_balance, 0.8, 1.2)
        self._slider(edits, "Volume", self.volume, 0.0, 2.0)
        auto_row = ttk.Frame(edits, style="Card.TFrame")
        auto_row.pack(fill="x", pady=(7, 0))
        ttk.Checkbutton(auto_row, text="Auto Correct", variable=self.auto_correct).pack(side="left")
        ttk.Checkbutton(auto_row, text="Auto White Balance", variable=self.auto_white_balance).pack(side="left", padx=8)
        preset_row = ttk.Frame(edits, style="Card.TFrame")
        preset_row.pack(fill="x", pady=(7, 0))
        ttk.Button(preset_row, text="Neutral", command=self.neutral).pack(side="left")
        ttk.Button(preset_row, text="Dolphin warm", command=self.dolphin_preset).pack(side="left", padx=6)

        logo = ttk.LabelFrame(right, text="4. TPS logo")
        logo.pack(fill="x")
        ttk.Checkbutton(logo, text="Add TPS logo — top left", variable=self.logo_enabled).pack(anchor="w")
        ttk.Label(logo, text="Fixed at 5% of video width • aspect ratio preserved • drop shadow", style="Card.TLabel", wraplength=340).pack(anchor="w", pady=(4, 0))

        self.run_button = ttk.Button(right, text="CREATE EDITED VIDEOS", style="Primary.TButton", command=self.start_processing)
        self.run_button.pack(fill="x", pady=(12, 4))
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
        scale = ttk.Scale(row, variable=variable, from_=low, to=high, command=lambda x, v=value: v.config(text=f"{float(x):.2f}"))
        scale.pack(side="left", fill="x", expand=True)
        value.config(text=f"{variable.get():.2f}")

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
        self.clips = [Clip(p, BooleanVar(value=True), StringVar(value="Ready"), DoubleVar(value=0)) for p in paths]
        self.refresh_tree()

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
            self.tree.insert("", "end", iid=str(index), values=("✓" if clip.selected.get() else "", clip.source.name, name, clip.status.get()))
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
        for var, value in [(self.exposure, 0), (self.contrast, 1), (self.warmth, 0), (self.red_balance, 1), (self.blue_balance, 1), (self.volume, 1)]:
            var.set(value)

    def dolphin_preset(self):
        self.exposure.set(0.08)
        self.contrast.set(1.05)
        self.warmth.set(0.06)
        self.red_balance.set(1.03)
        self.blue_balance.set(0.97)
        self.volume.set(1.0)

    def selected_clips(self):
        return [c for c in self.clips if c.selected.get()]

    def preview_selected(self):
        clips = self.selected_clips()
        if not clips:
            return messagebox.showinfo(APP_NAME, "Select a video first.")
        win = Toplevel(self.root)
        win.title("Batch preview — before and after")
        win.geometry("1120x760")
        selected_name = StringVar(value=clips[0].source.name)
        top = ttk.Frame(win, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="Video").pack(side="left")
        chooser = ttk.Combobox(top, textvariable=selected_name, values=[c.source.name for c in clips], state="readonly", width=55)
        chooser.pack(side="left", padx=8)
        status = ttk.Label(top, text="Loading preview…")
        status.pack(side="right")
        images = ttk.Frame(win, padding=10)
        images.pack(fill="both", expand=True)
        before = ttk.Label(images, text="BEFORE", anchor="center")
        before.pack(side="left", fill="both", expand=True, padx=(0, 5))
        after = ttk.Label(images, text="AFTER — export settings", anchor="center")
        after.pack(side="left", fill="both", expand=True, padx=(5, 0))
        timeline = DoubleVar(value=15)
        pending = {"job": None, "generation": 0}

        def schedule_render(_value=None, immediate=False):
            if pending["job"] is not None:
                win.after_cancel(pending["job"])
            delay = 0 if immediate else 350
            pending["job"] = win.after(delay, render)

        slider = ttk.Scale(win, variable=timeline, from_=0, to=100, command=schedule_render)
        slider.pack(fill="x", padx=18)
        ttk.Label(win, text="Move through the whole video. The BEFORE and AFTER images update automatically using the same corrections and logo as export.").pack(pady=(5, 12))

        def render():
            if not win.winfo_exists():
                return
            clip = next(c for c in clips if c.source.name == selected_name.get())
            status.config(text="Creating preview…")
            pending["generation"] += 1
            generation = pending["generation"]
            threading.Thread(target=self._render_preview_pair, args=(clip.source, timeline.get(), before, after, status, generation, pending), daemon=True).start()

        chooser.bind("<<ComboboxSelected>>", lambda _e: schedule_render(immediate=True))
        schedule_render(immediate=True)

    def _render_preview_pair(self, source: Path, percent: float, before_label, after_label, status_label, generation: int, pending):
        duration = self.video_duration(source)
        timestamp = max(0, duration * percent / 100.0)
        token = re.sub(r"\W+", "-", source.stem)[:35]
        temp = Path(tempfile.gettempdir())
        before_path = temp / f"tps-before-{token}.jpg"
        after_path = temp / f"tps-after-{token}.jpg"
        common = [ffmpeg_path(), "-y", "-ss", f"{timestamp:.3f}", "-i", str(source)]
        self.run_hidden(common + ["-frames:v", "1", "-vf", "scale=520:-2", str(before_path)], capture_output=True)
        self.run_hidden(self.preview_frame_command(source, timestamp, after_path), capture_output=True)
        if not before_path.exists() or not after_path.exists():
            self.root.after(0, lambda: status_label.config(text="Preview could not be created"))
            return
        def show():
            if generation != pending["generation"] or not before_label.winfo_exists():
                return
            before_image = Image.open(before_path); before_image.thumbnail((520, 580))
            after_image = Image.open(after_path); after_image.thumbnail((520, 580))
            before_photo = ImageTk.PhotoImage(before_image)
            after_photo = ImageTk.PhotoImage(after_image)
            before_label.config(image=before_photo, text="")
            after_label.config(image=after_photo, text="")
            before_label.image = before_photo
            after_label.image = after_photo
            status_label.config(text=f"{timestamp:.1f}s of {duration:.1f}s")
        self.root.after(0, show)

    def video_duration(self, source: Path) -> float:
        proc = self.run_hidden([ffmpeg_path(), "-i", str(source)], capture_output=True, text=True)
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
        if not match:
            return 1.0
        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

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
        output = Path(self.destination.get()) / clean_code(self.folder_name.get(), "TPS-Edited-Videos")
        if output.exists() and any(output.iterdir()):
            output = output.with_name(f"{output.name} {datetime.now():%Y-%m-%d %H%M%S}")
        output.mkdir(parents=True, exist_ok=False)
        low_output = None
        if self.low_res_enabled.get():
            low_parent = Path(self.low_destination.get() or self.destination.get())
            low_output = low_parent / f"{clean_code(self.folder_name.get(), 'TPS-Edited-Videos')}-low res"
            if low_output.exists() and any(low_output.iterdir()):
                low_output = low_output.with_name(f"{low_output.name} {datetime.now():%Y-%m-%d %H%M%S}")
            low_output.mkdir(parents=True, exist_ok=False)
        self.processing = True
        self.run_button.state(["disabled"])
        threading.Thread(target=self.process_batch, args=(output, low_output), daemon=True).start()

    def video_filter(self):
        brightness = max(-1.0, min(1.0, self.exposure.get() / 2.0))
        saturation = 1.0 + self.warmth.get() * 0.35
        filters = []
        if self.auto_correct.get():
            filters.append("normalize=blackpt=black:whitept=white:smoothing=50")
        if self.auto_white_balance.get():
            filters.append("grayworld")
        filters += [
            f"eq=brightness={brightness:.4f}:contrast={self.contrast.get():.4f}:saturation={saturation:.4f}",
            f"colorchannelmixer=rr={self.red_balance.get() + max(0, self.warmth.get()):.4f}:bb={self.blue_balance.get() + max(0, -self.warmth.get()):.4f}",
        ]
        return ",".join(filters)

    def preview_frame_command(self, source: Path, timestamp: float, target: Path):
        base_filter = self.video_filter() + ",scale=520:-2"
        if not self.logo_enabled.get():
            return [ffmpeg_path(), "-y", "-ss", f"{timestamp:.3f}", "-i", str(source), "-frames:v", "1", "-vf", base_filter, str(target)]
        logo_width = 26  # Exactly 5% of the 520px preview frame.
        margin = 8
        opacity = 0.92
        shadow = 0.70
        graph = (
            f"[0:v]{base_filter}[base];[1:v]format=rgba,colorchannelmixer=aa={opacity:.3f}[rawlogo];"
            f"[rawlogo]scale=w={logo_width}:h=-1:force_original_aspect_ratio=decrease,setsar=1[logo];"
            f"[logo]split[mark][shadowin];[shadowin]colorchannelmixer=rr=0:gg=0:bb=0:aa={shadow:.3f},boxblur=5[shadow];"
            f"[base][shadow]overlay={margin + 3}:{margin + 3}[shadowed];[shadowed][mark]overlay={margin}:{margin}[outv]"
        )
        return [ffmpeg_path(), "-y", "-ss", f"{timestamp:.3f}", "-i", str(source), "-loop", "1", "-i", self.logo_path.get(), "-filter_complex", graph, "-map", "[outv]", "-frames:v", "1", str(target)]

    def command(self, source: Path, target: Path, low_res: bool = False):
        base_filter = self.video_filter()
        if low_res:
            width, height = (int(v) for v in self.low_resolution.get().split("x"))
            base_filter += f",scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
        volume = f"volume={self.volume.get():.4f}"
        if not self.logo_enabled.get() and not low_res:
            return [ffmpeg_path(), "-y", "-i", str(source), "-vf", base_filter, "-af", volume, "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(target)]
        frame_width = int(self.low_resolution.get().split("x")[0]) if low_res else self.video_dimensions(source)[0]
        logo_width = max(16, round(frame_width * 0.05))
        margin = max(8, round(frame_width * 0.0125))
        opacity = 0.82 if low_res else 0.92
        shadow = 0.70
        graph = (
            f"[0:v]{base_filter}[base];"
            f"[1:v]format=rgba,colorchannelmixer=aa={opacity:.3f},scale=iw*main_w/iw*{width:.4f}:-1[logo];"
            f"[logo]split[mark][shadowin];[shadowin]colorchannelmixer=rr=0:gg=0:bb=0:aa={shadow:.3f},boxblur=8[shadow];"
            f"[base][shadow]overlay={margin + 5}:{margin + 5}[shadowed];[shadowed][mark]overlay={margin}:{margin}[outv]"
        )
        graph = (
            f"[0:v]{base_filter}[base];[1:v]format=rgba,colorchannelmixer=aa={opacity:.3f}[rawlogo];"
            f"[rawlogo]scale=w={logo_width}:h=-1:force_original_aspect_ratio=decrease,setsar=1[logo];"
            f"[logo]split[mark][shadowin];[shadowin]colorchannelmixer=rr=0:gg=0:bb=0:aa={shadow:.3f},boxblur=8[shadow];"
            f"[base][shadow]overlay={margin + 5}:{margin + 5}[shadowed];[shadowed][mark]overlay={margin}:{margin}[outv]"
        )
        return [ffmpeg_path(), "-y", "-i", str(source), "-loop", "1", "-i", self.logo_path.get(), "-filter_complex", graph, "-map", "[outv]", "-map", "0:a?", "-af", volume, "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(target)]

    def process_batch(self, output: Path, low_output: Path | None = None):
        selected = self.selected_clips()
        results = []
        for index, clip in enumerate(selected):
            target = output / self.output_name(index)
            self.root.after(0, lambda c=clip: c.status.set("Processing"))
            self.root.after(0, self.refresh_tree)
            proc = self.run_hidden(self.command(clip.source, target), capture_output=True, text=True)
            ok = proc.returncode == 0 and target.exists()
            low_target = None
            low_error = ""
            if ok and low_output is not None:
                low_target = low_output / self.output_name(index)
                low_proc = self.run_hidden(self.command(clip.source, low_target, low_res=True), capture_output=True, text=True)
                low_ok = low_proc.returncode == 0 and low_target.exists()
                ok = ok and low_ok
                if not low_ok:
                    low_error = low_proc.stderr[-2000:]
            status = "Complete" if ok else "Failed"
            clip.status.set(status)
            results.append({"source": str(clip.source), "output": str(target), "low_res_output": str(low_target) if low_target else None, "status": status, "error": "" if ok else (low_error or proc.stderr[-2000:])})
            self.root.after(0, lambda p=(index + 1) / len(selected) * 100: self.overall.configure(value=p))
            self.root.after(0, self.refresh_tree)
        (output / "TPS export summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        if low_output is not None:
            (low_output / "TPS export summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        completed = sum(r["status"] == "Complete" for r in results)
        self.processing = False
        self.root.after(0, lambda: self.run_button.state(["!disabled"]))
        folders = f"Full resolution:\n{output}"
        if low_output is not None:
            folders += f"\n\nLow resolution:\n{low_output}"
        self.root.after(0, lambda: self.summary.config(text=f"Finished: {completed} of {len(results)} videos created."))
        self.root.after(0, lambda: messagebox.showinfo(APP_NAME, f"Finished.\n\n{completed} of {len(results)} videos were created.\n\n{folders}"))


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

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import BooleanVar, DoubleVar, IntVar, StringVar, Tk, Toplevel, filedialog, messagebox, ttk

import imageio_ffmpeg
from PIL import Image, ImageTk

APP_NAME = "TPS Bulk Video Editor"
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
        self.root.title(APP_NAME)
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
        self.exposure = DoubleVar(value=0.0)
        self.contrast = DoubleVar(value=1.0)
        self.warmth = DoubleVar(value=0.0)
        self.red_balance = DoubleVar(value=1.0)
        self.blue_balance = DoubleVar(value=1.0)
        self.volume = DoubleVar(value=1.0)
        self.logo_enabled = BooleanVar(value=True)
        self.logo_path = StringVar(value=str(Path(__file__).with_name("assets") / "tps-logo.png"))
        self.logo_width = DoubleVar(value=18.0)
        self.logo_opacity = DoubleVar(value=0.92)
        self.logo_margin = IntVar(value=28)
        self.shadow = DoubleVar(value=0.70)

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
        ttk.Label(header, text="TPS BULK VIDEO EDITOR", style="Title.TLabel", padding=(14, 8)).pack(side="left")
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
        self._field(naming, "Date", self.job_date)
        self._field(naming, "Activity code", self.activity)
        self._field(naming, "Photographer initials", self.photographer)
        self._field(naming, "Starting number", self.start_number)
        self._field(naming, "New folder name", self.folder_name)
        dest_row = ttk.Frame(naming, style="Card.TFrame")
        dest_row.pack(fill="x", pady=4)
        ttk.Entry(dest_row, textvariable=self.destination).pack(side="left", fill="x", expand=True)
        ttk.Button(dest_row, text="Destination", command=self.choose_destination).pack(side="left", padx=(6, 0))

        edits = ttk.LabelFrame(right, text="3. Bulk adjustments")
        edits.pack(fill="x", pady=10)
        self._slider(edits, "Exposure", self.exposure, -1.5, 1.5)
        self._slider(edits, "Contrast", self.contrast, 0.7, 1.4)
        self._slider(edits, "Warmth", self.warmth, -0.3, 0.3)
        self._slider(edits, "Red balance", self.red_balance, 0.8, 1.2)
        self._slider(edits, "Blue balance", self.blue_balance, 0.8, 1.2)
        self._slider(edits, "Volume", self.volume, 0.0, 2.0)
        preset_row = ttk.Frame(edits, style="Card.TFrame")
        preset_row.pack(fill="x", pady=(7, 0))
        ttk.Button(preset_row, text="Neutral", command=self.neutral).pack(side="left")
        ttk.Button(preset_row, text="Dolphin warm", command=self.dolphin_preset).pack(side="left", padx=6)

        logo = ttk.LabelFrame(right, text="4. TPS logo")
        logo.pack(fill="x")
        ttk.Checkbutton(logo, text="Add logo top left", variable=self.logo_enabled).pack(anchor="w")
        logo_row = ttk.Frame(logo, style="Card.TFrame")
        logo_row.pack(fill="x", pady=4)
        ttk.Entry(logo_row, textvariable=self.logo_path).pack(side="left", fill="x", expand=True)
        ttk.Button(logo_row, text="Choose", command=self.choose_logo).pack(side="left", padx=(6, 0))
        self._slider(logo, "Width %", self.logo_width, 8, 30)
        self._slider(logo, "Opacity", self.logo_opacity, 0.25, 1.0)
        self._slider(logo, "Shadow", self.shadow, 0.0, 1.0)

        self.run_button = ttk.Button(right, text="CREATE EDITED VIDEOS", style="Primary.TButton", command=self.start_processing)
        self.run_button.pack(fill="x", pady=(12, 4))
        self.overall = ttk.Progressbar(right, maximum=100)
        self.overall.pack(fill="x")
        self.summary = ttk.Label(right, text="Original SD-card files are never changed.", wraplength=360)
        self.summary.pack(fill="x", pady=7)

        for variable in (self.job_date, self.activity, self.photographer, self.start_number):
            variable.trace_add("write", lambda *_: self.refresh_tree())

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
            self.load_clips(Path(path))

    def choose_destination(self):
        path = filedialog.askdirectory(title="Choose where the new folder will be created")
        if path:
            self.destination.set(path)

    def choose_logo(self):
        path = filedialog.askopenfilename(title="Choose transparent logo", filetypes=[("PNG image", "*.png")])
        if path:
            self.logo_path.set(path)

    def load_clips(self, folder: Path):
        paths = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS)
        self.clips = [Clip(p, BooleanVar(value=True), StringVar(value="Ready"), DoubleVar(value=0)) for p in paths]
        self.refresh_tree()

    def output_name(self, index: int) -> str:
        date = clean_code(self.job_date.get(), datetime.now().strftime("%d-%b-%Y"))
        activity = clean_code(self.activity.get(), "VIDEO")
        photographer = clean_code(self.photographer.get(), "TPS")
        return f"{date}-{activity}-{photographer}-{self.start_number.get() + index:04d}.MP4"

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
        clip = clips[0]
        tmp = Path(tempfile.gettempdir()) / "tps-video-preview.jpg"
        subprocess.run([ffmpeg_path(), "-y", "-ss", "5", "-i", str(clip.source), "-frames:v", "1", str(tmp)], capture_output=True)
        if not tmp.exists():
            return messagebox.showerror(APP_NAME, "A preview could not be created for this clip.")
        win = Toplevel(self.root)
        win.title(f"Preview — {clip.source.name}")
        image = Image.open(tmp)
        image.thumbnail((1000, 620))
        self.preview_image = ImageTk.PhotoImage(image)
        ttk.Label(win, image=self.preview_image).pack(padx=12, pady=12)
        ttk.Label(win, text="Preview frame only. Final adjustments are applied during export.").pack(pady=(0, 12))

    def validate(self):
        if not self.selected_clips():
            return "Select at least one video."
        if not self.destination.get():
            return "Choose an output destination."
        if self.logo_enabled.get() and not Path(self.logo_path.get()).exists():
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
        self.processing = True
        self.run_button.state(["disabled"])
        threading.Thread(target=self.process_batch, args=(output,), daemon=True).start()

    def video_filter(self):
        brightness = max(-1.0, min(1.0, self.exposure.get() / 2.0))
        saturation = 1.0 + self.warmth.get() * 0.35
        filters = [
            f"eq=brightness={brightness:.4f}:contrast={self.contrast.get():.4f}:saturation={saturation:.4f}",
            f"colorchannelmixer=rr={self.red_balance.get() + max(0, self.warmth.get()):.4f}:bb={self.blue_balance.get() + max(0, -self.warmth.get()):.4f}",
        ]
        return ",".join(filters)

    def command(self, source: Path, target: Path):
        base_filter = self.video_filter()
        volume = f"volume={self.volume.get():.4f}"
        if not self.logo_enabled.get():
            return [ffmpeg_path(), "-y", "-i", str(source), "-vf", base_filter, "-af", volume, "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(target)]
        width = self.logo_width.get() / 100.0
        margin = self.logo_margin.get()
        opacity = self.logo_opacity.get()
        shadow = self.shadow.get()
        graph = (
            f"[0:v]{base_filter}[base];"
            f"[1:v]format=rgba,colorchannelmixer=aa={opacity:.3f},scale=iw*main_w/iw*{width:.4f}:-1[logo];"
            f"[logo]split[mark][shadowin];[shadowin]colorchannelmixer=rr=0:gg=0:bb=0:aa={shadow:.3f},boxblur=8[shadow];"
            f"[base][shadow]overlay={margin + 5}:{margin + 5}[shadowed];[shadowed][mark]overlay={margin}:{margin}[outv]"
        )
        # scale2ref allows logo width to follow each source's original resolution.
        graph = (
            f"[0:v]{base_filter}[base];[1:v]format=rgba,colorchannelmixer=aa={opacity:.3f}[rawlogo];"
            f"[rawlogo][base]scale2ref=w=main_w*{width:.4f}:h=-1[logo][base2];"
            f"[logo]split[mark][shadowin];[shadowin]colorchannelmixer=rr=0:gg=0:bb=0:aa={shadow:.3f},boxblur=8[shadow];"
            f"[base2][shadow]overlay={margin + 5}:{margin + 5}[shadowed];[shadowed][mark]overlay={margin}:{margin}[outv]"
        )
        return [ffmpeg_path(), "-y", "-i", str(source), "-loop", "1", "-i", self.logo_path.get(), "-filter_complex", graph, "-map", "[outv]", "-map", "0:a?", "-af", volume, "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(target)]

    def process_batch(self, output: Path):
        selected = self.selected_clips()
        results = []
        for index, clip in enumerate(selected):
            target = output / self.output_name(index)
            self.root.after(0, lambda c=clip: c.status.set("Processing"))
            self.root.after(0, self.refresh_tree)
            proc = subprocess.run(self.command(clip.source, target), capture_output=True, text=True)
            ok = proc.returncode == 0 and target.exists()
            status = "Complete" if ok else "Failed"
            clip.status.set(status)
            results.append({"source": str(clip.source), "output": str(target), "status": status, "error": "" if ok else proc.stderr[-2000:]})
            self.root.after(0, lambda p=(index + 1) / len(selected) * 100: self.overall.configure(value=p))
            self.root.after(0, self.refresh_tree)
        (output / "TPS export summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        completed = sum(r["status"] == "Complete" for r in results)
        self.processing = False
        self.root.after(0, lambda: self.run_button.state(["!disabled"]))
        self.root.after(0, lambda: self.summary.config(text=f"Finished: {completed} of {len(results)} videos created in {output}"))
        self.root.after(0, lambda: messagebox.showinfo(APP_NAME, f"Finished.\n\n{completed} of {len(results)} videos were created.\n\nOutput folder:\n{output}"))


if __name__ == "__main__":
    root = Tk()
    TPSVideoEditor(root)
    root.mainloop()


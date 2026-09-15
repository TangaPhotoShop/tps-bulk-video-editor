# TPS Bulk Video Editor

Current Windows release: **Version 1.3.11**. The installed version is displayed in the application title bar and main header, and every installer filename includes its version.

Windows desktop software for Tangalooma Photo Shop. It reads tour videos directly from an SD card or local folder, applies bulk video/audio adjustments, overlays the TPS logo, renames clips and writes MP4 files into a newly created output folder. Original source files are never changed.

## Filename pattern

`DD-MMM-YYYY-ACTIVITY-PHOTOGRAPHER-0001.MP4`

Example: `13-SEP-2026-DOL-DM-0001.MP4`

Enter the first complete output filename once. The app derives the new folder name from its prefix and increments the trailing number for every selected video, preserving three- or four-digit numbering.

## Batch correction and preview

- **Auto Correct** normalizes exposure and tonal range through the full video.
- **Auto White Balance** uses a conservative grey-edge correction that preserves skin tones and avoids strong yellow/green shifts in night footage.
- Manual controls cover white balance, exposure, contrast, shadows, highlights, blacks, whites, warmth, saturation and volume.
- Automatic options and manual sliders apply to every frame of every selected clip.
- The permanent before/after preview is embedded in the main window and refreshes automatically as staff move a slider, toggle automatic correction, select a video or move through its timeline.
- Logo, audio, correction and sequential naming settings apply to the entire selected batch.

Version 1.3.11 also fixes the Windows processing handoff that could leave a job at 0%, fixes full-resolution logo command generation, and reports processing failures visibly instead of silently stopping.

## Low-resolution upload copies

Optionally create a second watermarked batch for uploading. Choose 640x480, 854x480 or 1280x720 and an optional separate destination. The app preserves aspect ratio with padding (no stretching), keeps the same sequential filenames, and writes the copies to a second folder named `<filename-prefix>-low res`.

## Windows build

Run `build-windows.ps1`. The resulting Windows installer is created in `dist/`. It uses a stable application identity so future installers detect the existing installation, close the running app, remove the previous executable and replace it with the new release. The installer creates Start menu and optional desktop shortcuts using the TPS video-camera icon.

The upgrade also clears the previous application folder and removes the specifically named pre-installer portable executable from the current user's Desktop and Downloads. It does not touch videos, export folders or downloaded setup files.

Only one editor window can run at a time. When an earlier installed version is detected, Setup asks for confirmation before removing it and installing the replacement.

## Railway

The root Node server hosts the download and product information page. Video processing remains entirely local on Windows; videos are never uploaded to Railway.

## Privacy

Test footage, SD-card contents and exported customer videos are excluded from Git and must not be committed.

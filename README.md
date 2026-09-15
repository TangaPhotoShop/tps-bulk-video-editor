# TPS Bulk Video Editor

Current Windows release: **Version 1.3.16**. The installed version is displayed in the application title bar and main header, and every installer filename includes its version.

Windows desktop software for Tangalooma Photo Shop. It reads tour videos directly from an SD card or local folder, applies bulk video/audio adjustments, overlays the TPS logo, renames clips and writes MP4 files into a newly created output folder. Original source files are never changed.

## Filename pattern

`DD-MMM-YYYY-DOL-PHOTOGRAPHER-0001.MP4`

Example: `13-SEP-2026-DOL-DM-0001.MP4`

Enter the date and photographer initials; the starting number defaults to `0001`. The activity is locked to DOL, and the app generates filenames such as `15-SEP-2026-DOL-SW-0001.MP4` plus the folder name `15-SEP-2026-DOL_SW`. Every selected video receives the next four-digit number.

## Batch correction and preview

- **Auto Exposure** analyses exposure continuously and smooths changes between neighbouring frames to avoid flicker and brightness pumping.
- **Auto White Balance** uses a conservative grey-edge correction that preserves skin tones and avoids strong yellow/green shifts in night footage.
- Manual controls cover white balance, exposure, contrast, shadows, highlights, blacks, whites, warmth, saturation and volume.
- Automatic options and manual sliders apply to every frame of every selected clip.
- The permanent before/after preview is embedded in the main window and refreshes automatically as staff move a slider, toggle automatic correction, select a video or move through its timeline.
- Logo, audio, correction and sequential naming settings apply to the entire selected batch.

Version 1.3.11 fixed the Windows processing handoff that could leave a job at 0%, fixed full-resolution logo command generation, and made processing failures visible.

Version 1.3.12 speeds up full-resolution encoding while retaining high-quality output, uses all available FFmpeg CPU threads, and creates the low-resolution copy from the completed edited master when possible instead of repeating every correction. The file list now has vertical and horizontal scrollbars. Volume can be boosted to 4× for quiet nights, with a peak limiter above the original level to reduce clipping.

Version 1.3.13 adds a prominent busy export notice with the current file, stage and percentage. Staff can stop an export after confirming; completed videos remain in place and the incomplete current output is removed safely.

Version 1.3.14 displays the original, full-resolution and low-resolution file sizes, updating output sizes while each file is written. It adds a 0–100 export-quality control (85% default), estimated time remaining, a destination free-space check and an Open Output Folder button.

Version 1.3.15 keeps batch cancellation permanently visible: the main Create Edited Videos button becomes a red Cancel Batch Export button while processing. Cancellation requires confirmation, preserves completed outputs, removes the incomplete current file and then restores the editor for adjustment and restart.

Version 1.3.16 adds a low-resolution watermarked-only export mode that skips the full-resolution batch. It replaces free-form filenames with validated Date, fixed DOL activity, Photographer Initials and Starting Number controls, generating locked sequential names such as `15-SEP-2026-DOL-SW-0001.MP4`. The high- and low-resolution destination controls are editable drop-downs that suggest the standard TPS Z: drive folders while retaining Browse and manual overrides. The file list numbers every source clip and shows its detected current resolution, expected full-resolution output and selected low-resolution output. During export, the BUSY notice reports the current video count, whole-batch percentage and estimated time remaining.

## Low-resolution upload copies

Optionally create a second watermarked batch for uploading, or choose low-resolution only to skip the full-resolution batch. Choose 640x480, 854x480 or 1280x720 and an optional separate destination. The app preserves aspect ratio with padding (no stretching), keeps the same sequential filenames, and writes the copies to a folder such as `15-SEP-2026-DOL_SW-LOW-RES`.

## Windows build

Run `build-windows.ps1`. The resulting Windows installer is created in `dist/`. It uses a stable application identity so future installers detect the existing installation, close the running app, remove the previous executable and replace it with the new release. The installer creates Start menu and optional desktop shortcuts using the TPS video-camera icon.

The upgrade also clears the previous application folder and removes the specifically named pre-installer portable executable from the current user's Desktop and Downloads. It does not touch videos, export folders or downloaded setup files.

Only one editor window can run at a time. When an earlier installed version is detected, Setup asks for confirmation before removing it and installing the replacement.

## Railway

The root Node server hosts the download and product information page. Video processing remains entirely local on Windows; videos are never uploaded to Railway.

## Privacy

Test footage, SD-card contents and exported customer videos are excluded from Git and must not be committed.

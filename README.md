# TPS Bulk Video Editor

Windows desktop software for Tangalooma Photo Shop. It reads tour videos directly from an SD card or local folder, applies bulk video/audio adjustments, overlays the TPS logo, renames clips and writes MP4 files into a newly created output folder. Original source files are never changed.

## Filename pattern

`DD-MMM-YYYY-ACTIVITY-PHOTOGRAPHER-0001.MP4`

Example: `13-SEP-2026-DOL-DM-0001.MP4`

Enter the first complete output filename once. The app derives the new folder name from its prefix and increments the trailing number for every selected video, preserving three- or four-digit numbering.

## Batch correction and preview

- **Auto Correct** normalizes exposure and tonal range through the full video.
- **Auto White Balance** corrects colour balance through the full video.
- Both options work with manual sliders and apply to every frame of every selected clip.
- The built-in before/after preview lets staff choose any selected video and scrub from the beginning to the end before exporting.
- Logo, audio, correction and sequential naming settings apply to the entire selected batch.

## Windows build

Run `build-windows.ps1`. The resulting standalone executable is created in `dist/`.

## Railway

The root Node server hosts the download and product information page. Video processing remains entirely local on Windows; videos are never uploaded to Railway.

## Privacy

Test footage, SD-card contents and exported customer videos are excluded from Git and must not be committed.

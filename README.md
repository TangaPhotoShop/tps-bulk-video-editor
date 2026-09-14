# TPS Bulk Video Editor

Windows desktop software for Tangalooma Photo Shop. It reads tour videos directly from an SD card or local folder, applies bulk video/audio adjustments, overlays the TPS logo, renames clips and writes MP4 files into a newly created output folder. Original source files are never changed.

## Filename pattern

`DD-MMM-YYYY-ACTIVITY-PHOTOGRAPHER-0001.MP4`

Example: `13-SEP-2026-DOL-DM-0001.MP4`

## Windows build

Run `build-windows.ps1`. The resulting standalone executable is created in `dist/`.

## Railway

The root Node server hosts the download and product information page. Video processing remains entirely local on Windows; videos are never uploaded to Railway.

## Privacy

Test footage, SD-card contents and exported customer videos are excluded from Git and must not be committed.

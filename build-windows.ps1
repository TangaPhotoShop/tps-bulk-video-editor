$ErrorActionPreference = "Stop"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item "branding/TPS_hiRes.png" "desktop/assets/tps-logo.png" -Force
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "TPS Bulk Video Editor" --icon "branding/tps-video-editor-icon.ico" --add-data "desktop/assets/tps-logo.png;assets" --collect-all imageio_ffmpeg desktop/app.py

$iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) {
    choco install innosetup --yes --no-progress
}
& $iscc installer.iss
Write-Host "Built: dist/TPS Bulk Video Editor Setup.exe"

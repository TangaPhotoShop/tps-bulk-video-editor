$ErrorActionPreference = "Stop"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item "branding/TPS_hiRes.png" "desktop/assets/tps-logo.png" -Force
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "TPS Bulk Video Editor" --add-data "desktop/assets/tps-logo.png;assets" --collect-all imageio_ffmpeg desktop/app.py
Write-Host "Built: dist/TPS Bulk Video Editor.exe"


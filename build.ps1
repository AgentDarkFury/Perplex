# Build a single-file, windowless Perplex.exe into .\dist
Set-Location $PSScriptRoot
$ErrorActionPreference = "Stop"

if (-not (Test-Path .venv)) { python -m venv .venv }
.venv/Scripts/python -m pip install -q -r requirements.txt pyinstaller

New-Item -ItemType Directory -Force build | Out-Null
.venv/Scripts/python -m perplex.icon build/perplex.ico

.venv/Scripts/python -m PyInstaller `
    --noconfirm --clean --onefile --windowed `
    --name Perplex `
    --icon build/perplex.ico `
    --paths . `
    --exclude-module tkinter `
    packaging/perplex_tray.py

Write-Host "Built dist\Perplex.exe"

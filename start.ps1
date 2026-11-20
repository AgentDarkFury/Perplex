# Run Perplex from the repository folder
Set-Location $PSScriptRoot

if (-not (Test-Path .venv)) {
    python -m venv .venv
    .venv/Scripts/python -m pip install -r requirements.txt
}

.venv/Scripts/python -m perplex

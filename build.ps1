# Build local de LogiBatt.exe (Windows, Python 3.10+)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .venv)) { python -m venv .venv }
.\.venv\Scripts\python -m pip install -q --disable-pip-version-check -r requirements.txt pyinstaller

.\.venv\Scripts\pyinstaller --noconfirm --clean --noconsole --onefile `
    --name LogiBatt --icon assets\logibatt.ico `
    --hidden-import pystray._win32 logibatt.py

Write-Host "`nOK -> dist\LogiBatt.exe" -ForegroundColor Green

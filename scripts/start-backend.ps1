# Start the Django backend on http://127.0.0.1:8000 (foreground).
# Prints the one-time operator setup token on first run.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$venvPython = Join-Path $root '.venv/Scripts/python.exe'
if (-not (Test-Path $venvPython)) {
  Write-Error "No .venv found. Run ./scripts/setup.ps1 first."
  exit 1
}

& $venvPython backend/manage.py migrate
& $venvPython backend/manage.py setup_token
& $venvPython backend/manage.py runserver 127.0.0.1:8000

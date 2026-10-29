# Serve synthetic fixtures on http://127.0.0.1:8901 (foreground).
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$venvPython = Join-Path $root '.venv/Scripts/python.exe'
if (Test-Path $venvPython) {
  & $venvPython fixtures/serve.py
} else {
  python fixtures/serve.py
}

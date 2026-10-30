# Initial setup: Python venv + backend deps + Node deps for both apps.
# Run once from the repo root:  ./scripts/setup.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Require-Command($name, $hint) {
  if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
    Write-Error "Missing '$name'. $hint"
    exit 1
  }
}

Require-Command python 'Install Python 3.13+ from https://www.python.org/downloads/'
Require-Command node 'Install Node.js 20+ from https://nodejs.org/'

$pyVersion = (python --version 2>&1) -replace 'Python ', ''
Write-Host "Python $pyVersion / Node $(node --version)"

if (-not (Test-Path '.venv/Scripts/python.exe')) {
  Write-Host 'Creating .venv ...'
  if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv venv .venv
    $pip = 'uv pip'
  } else {
    python -m venv .venv
    $pip = ''
  }
} else {
  $pip = if (Get-Command uv -ErrorAction SilentlyContinue) { 'uv pip' } else { '' }
}
$venvPython = Join-Path $root '.venv/Scripts/python.exe'

Write-Host 'Installing backend dependencies ...'
if ($pip -eq 'uv pip') {
  uv pip install --python $venvPython -r backend/requirements-dev.lock
} else {
  & $venvPython -m pip install -r backend/requirements-dev.lock
}

Write-Host 'Applying database migrations ...'
& $venvPython backend/manage.py migrate

Write-Host 'Installing mother-app dependencies ...'
npm install --prefix apps/mother
Write-Host 'Installing extension dependencies ...'
npm install --prefix apps/extension

Write-Host ''
Write-Host 'Setup complete. Start everything with:  ./scripts/dev.ps1'

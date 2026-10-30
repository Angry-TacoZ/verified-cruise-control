# Full local verification: backend + mother + extension gates.
# Run from the repo root:  ./scripts/test.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$venvPython = Join-Path $root '.venv/Scripts/python.exe'
if (-not (Test-Path $venvPython)) {
  Write-Error "No .venv found. Run ./scripts/setup.ps1 first."
  exit 1
}

Write-Host '=== backend: ruff + pytest ==='
& $venvPython -m ruff check backend/
& $venvPython -m ruff format --check backend/
& $venvPython -m pytest backend/ -q

Write-Host '=== mother: check + lint + test + build ==='
npm run check --prefix apps/mother
npm run lint --prefix apps/mother
npm test --prefix apps/mother
npm run build --prefix apps/mother

Write-Host '=== extension: check + lint + test + build ==='
npm run check --prefix apps/extension
npm run lint --prefix apps/extension
npm test --prefix apps/extension
npm run build --prefix apps/extension

Write-Host ''
Write-Host 'All gates passed.'

# Start backend + audit worker + mother app + fixture server, each in its own window.
# Run from the repo root:  ./scripts/dev.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

Write-Host 'Starting backend (127.0.0.1:8000) ...'
Start-Process powershell -ArgumentList '-NoExit', '-File', (Join-Path $root 'scripts/start-backend.ps1')
Write-Host 'Starting audit worker ...'
Start-Process powershell -ArgumentList '-NoExit', '-File', (Join-Path $root 'scripts/start-audit-worker.ps1')
Write-Host 'Starting mother app (127.0.0.1:5173) ...'
Start-Process powershell -ArgumentList '-NoExit', '-File', (Join-Path $root 'scripts/start-mother.ps1')
Write-Host 'Starting fixture server (127.0.0.1:8901) ...'
Start-Process powershell -ArgumentList '-NoExit', '-File', (Join-Path $root 'scripts/start-fixtures.ps1')

Write-Host ''
Write-Host 'Open the mother app at:  http://127.0.0.1:5173'
Write-Host 'Stop everything with:    ./scripts/stop-dev.ps1'

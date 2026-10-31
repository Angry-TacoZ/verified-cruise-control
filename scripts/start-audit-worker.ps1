# Start the standalone audit worker (foreground).
# Polls the audit queue and runs claimed jobs; the web server never
# processes audits itself. Ctrl+C finishes the current job then stops;
# a second Ctrl+C abandons it (lease reclaim resumes it later).
# No spend happens without a queued job, VCC_INFERENCE_ENABLED=1, and keys.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$venvPython = Join-Path $root '.venv/Scripts/python.exe'
if (-not (Test-Path $venvPython)) {
  Write-Error "No .venv found. Run ./scripts/setup.ps1 first."
  exit 1
}

& $venvPython backend/manage.py migrate
& $venvPython backend/manage.py run_audit_worker

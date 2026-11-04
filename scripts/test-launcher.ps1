# Windows integration test for the Install/Start/Stop launchers.
#
# Run from the repo root:  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test-launcher.ps1
# Windows-only (uses Win32_Process, Get-NetTCPConnection, LOCALAPPDATA).
#
# Isolated by design: test ports + a temp state dir + no browser, so the
# developer's default services (8000/5173) are never touched. Exits non-zero
# on the first failure. Never kills anything it did not start, and asserts
# an unrelated sleeper process survives Stop.
#
# This test runs the real Install (converging setup + extension build),
# real Start/Stop cycles, a port-conflict drill, and stale-state recovery
# -- all against live processes on this machine.

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

$testApiPort = 8021
$testWebPort = 5191
$conflictApiPort = 8022
$conflictWebPort = 5192
$testStateDir = Join-Path $env:TEMP 'vcc-launcher-test'

$failures = 0
function Assert($condition, $label) {
  if ($condition) {
    Write-Host "  [PASS] $label" -ForegroundColor Green
  } else {
    Write-Host "  [FAIL] $label" -ForegroundColor Red
    $script:failures += 1
  }
}

function Invoke-Launcher($action, $apiPort, $webPort, $stateDir) {
  $env:VCC_API_PORT = "$apiPort"
  $env:VCC_WEB_PORT = "$webPort"
  $env:VCC_NO_BROWSER = '1'
  $env:VCC_LAUNCHER_STATE_DIR = $stateDir
  $env:VCC_LAUNCHER_WAIT_SEC = '25'
  try {
    # Capture the information stream (Write-Host) only: merging stderr
    # (2>&1) would promote informational native stderr (uv, npm, vite) to
    # terminating errors under a Stop preference. Native stderr stays on
    # the console where it belongs.
    $out = & (Join-Path $root 'scripts/launcher.ps1') -Action $action -NonInteractive 6>&1 | Out-String
    return @{ code = $LASTEXITCODE; output = $out }
  } finally {
    Remove-Item Env:VCC_API_PORT -ErrorAction SilentlyContinue
    Remove-Item Env:VCC_WEB_PORT -ErrorAction SilentlyContinue
    Remove-Item Env:VCC_NO_BROWSER -ErrorAction SilentlyContinue
    Remove-Item Env:VCC_LAUNCHER_STATE_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:VCC_LAUNCHER_WAIT_SEC -ErrorAction SilentlyContinue
  }
}

function Get-OwnedPids($stateDir) {
  $file = Join-Path $stateDir 'services.json'
  if (-not (Test-Path $file)) { return @() }
  return @( (Get-Content $file -Raw | ConvertFrom-Json) | ForEach-Object { [int]$_.pid } )
}

function Reset-StateDir {
  if (Test-Path $testStateDir) { Remove-Item $testStateDir -Recurse -Force }
}

Write-Host ''
Write-Host '== 1. First-time setup (real Install: converge + build + verify)'

# Clean slate for services, but Install itself must converge on the real repo.
Reset-StateDir
$install = Invoke-Launcher 'Install' $testApiPort $testWebPort $testStateDir
Assert ($install.code -eq 0) "first Install exits 0 (got $($install.code))"
Assert (Test-Path (Join-Path $root 'apps/extension/.output/chrome-mv3/manifest.json')) 'extension manifest exists'
Assert (Test-Path (Join-Path $root 'apps/extension/.output/chrome-mv3/background.js')) 'extension background bundle exists'
Assert (Test-Path (Join-Path $root 'apps/extension/.output/chrome-mv3/popup.html')) 'extension popup exists'
Assert (Test-Path (Join-Path $root 'apps/extension/.output/chrome-mv3/content-scripts')) 'content scripts exist'

Write-Host ''
Write-Host '== 2. Repeat setup converges without destruction'
$install2 = Invoke-Launcher 'Install' $testApiPort $testWebPort $testStateDir
Assert ($install2.code -eq 0) "second Install exits 0 (got $($install2.code))"
Assert (Test-Path (Join-Path $root 'apps/extension/.output/chrome-mv3/manifest.json')) 'extension output intact after repeat'

Write-Host ''
Write-Host '== 3. Start brings all services to ready'
$start = Invoke-Launcher 'Start' $testApiPort $testWebPort $testStateDir
Assert ($start.code -eq 0 -or $start.code -eq 2) "Start exits 0 (or 2 for first-time token) (got $($start.code))"
Assert ($start.output -match 'backend ready') 'backend readiness reported'
Assert ($start.output -match 'mother app ready') 'mother readiness reported'
Assert ($start.output -match 'Inference: DISABLED') 'inference reported disabled by default'
$pids1 = Get-OwnedPids $testStateDir
Assert ($pids1.Count -eq 3) "three services tracked (got $($pids1.Count))"
$httpOk = $false
try {
  $r = Invoke-WebRequest -Uri "http://127.0.0.1:${testApiPort}/api/health" -UseBasicParsing -TimeoutSec 10
  $httpOk = $r.StatusCode -eq 200
} catch { $httpOk = $false }
Assert $httpOk 'backend answers /api/health on the test port'
$stateJson = Get-Content (Join-Path $testStateDir 'services.json') -Raw
Assert ($stateJson -notmatch '(?i)token') 'state file carries no tokens'
foreach ($procId in $pids1) {
  $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$procId").CommandLine
  Assert ("$cmd" -match [regex]::Escape($root)) "PID $procId command line is inside this repo"
}

Write-Host ''
Write-Host '== 4. Second Start adds no duplicate services'
$start2 = Invoke-Launcher 'Start' $testApiPort $testWebPort $testStateDir
Assert ($start2.code -eq 0 -or $start2.code -eq 2) "repeat Start exits 0/2 (got $($start2.code))"
$pids2 = Get-OwnedPids $testStateDir
Assert (($pids1 | Sort-Object) -join ',' -eq ($pids2 | Sort-Object) -join ',') 'repeat Start reuses the same PIDs'
Assert ($start2.output -match 'already running') 'repeat Start reports already-running services'

Write-Host ''
Write-Host '== 5. Occupied port is reported, never killed'
$dummy = Start-Process -FilePath 'python' -ArgumentList '-m', 'http.server', "$conflictApiPort", '--bind', '127.0.0.1' `
  -WindowStyle Minimized -PassThru
Start-Sleep -Seconds 3
$conflictStateDir = Join-Path $env:TEMP 'vcc-launcher-conflict'
if (Test-Path $conflictStateDir) { Remove-Item $conflictStateDir -Recurse -Force }
$conflict = Invoke-Launcher 'Start' $conflictApiPort $conflictWebPort $conflictStateDir
Assert ($conflict.code -eq 1) "conflicted Start exits 1 (got $($conflict.code))"
Assert ($conflict.output -match "$conflictApiPort.*already in use") 'conflict names the occupied port'
Assert ($conflict.output -match 'never stops processes it did not start') 'conflict promises no kill'
$dummyAlive = $null -ne (Get-Process -Id $dummy.Id -ErrorAction SilentlyContinue)
Assert $dummyAlive 'dummy port owner survives the conflicted Start'
Assert (-not (Test-Path (Join-Path $conflictStateDir 'services.json'))) 'no state recorded for the failed Start'
Stop-Process -Id $dummy.Id -Force
if (Test-Path $conflictStateDir) { Remove-Item $conflictStateDir -Recurse -Force }

Write-Host ''
Write-Host '== 6. Stop affects only launcher-owned processes'
$sleeper = Start-Process -FilePath 'ping' -ArgumentList '-n', '600', '127.0.0.1' `
  -WindowStyle Minimized -PassThru
$stop = Invoke-Launcher 'Stop' $testApiPort $testWebPort $testStateDir
Assert ($stop.code -eq 0) "Stop exits 0 (got $($stop.code))"
Start-Sleep -Seconds 3
foreach ($procId in $pids1) {
  Assert ($null -eq (Get-Process -Id $procId -ErrorAction SilentlyContinue)) "tracked PID $procId is gone"
}
$sleeperAlive = $null -ne (Get-Process -Id $sleeper.Id -ErrorAction SilentlyContinue)
Assert $sleeperAlive 'unrelated sleeper process survives Stop'
Stop-Process -Id $sleeper.Id -Force
Assert (-not (Test-Path (Join-Path $testStateDir 'services.json'))) 'state file cleaned after full stop'

Write-Host ''
Write-Host '== 7. Stale state recovers on next Start'
$stale = Invoke-Launcher 'Start' $testApiPort $testWebPort $testStateDir
Assert ($stale.code -eq 0 -or $stale.code -eq 2) "Start after Stop exits 0/2 (got $($stale.code))"
$fresh = Get-OwnedPids $testStateDir
Assert ($fresh.Count -eq 3) 'all three services restarted'
$final = Invoke-Launcher 'Stop' $testApiPort $testWebPort $testStateDir
Assert ($final.code -eq 0) 'final Stop exits 0'
Reset-StateDir

Write-Host ''
if ($failures -gt 0) {
  Write-Host "$failures assertion(s) FAILED." -ForegroundColor Red
  exit 1
}
Write-Host 'Launcher integration: all assertions passed.' -ForegroundColor Green
exit 0

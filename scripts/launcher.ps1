# Verified Cruise Control -- Windows launcher engine.
#
# One shared implementation behind three double-clickable wrappers in the
# repo root (Install / Start / Stop Verified Cruise Control.cmd). Plain
# PowerShell, no new dependencies, no admin rights, no Windows services.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/launcher.ps1 -Action Install|Start|Stop [-NonInteractive]
#
# Environment overrides (tests and port conflicts; production defaults keep
# the extension manifest and mother-app proxy consistent):
#   VCC_API_PORT      Django backend port (default 8000)
#   VCC_WEB_PORT      mother-app (Vite) port (default 5173)
#   VCC_NO_BROWSER=1  skip opening the browser (tests)
#   VCC_LAUNCHER_STATE_DIR  override the per-user state directory (tests)
#
# Ownership: every service the launcher starts is recorded (PID, start
# time, executable path, command-line marker) in a per-user state file.
# Stop verifies identity before killing, so unrelated Python/Node/Django/
# Vite processes -- including ones started by scripts/dev.ps1 -- are never
# touched. Ports are never killed by: an occupied port is reported with
# its owning process and safe recovery steps instead.
#
# The launcher never enables paid inference. It starts the backend with
# the ambient environment untouched (VCC_INFERENCE_ENABLED stays off
# unless James set it himself) and reports the effective setting.

param(
  [Parameter(Mandatory = $true)]
  [ValidateSet('Install', 'Start', 'Stop')]
  [string]$Action,
  [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

$apiPort = if ($env:VCC_API_PORT) { [int]$env:VCC_API_PORT } else { 8000 }
$webPort = if ($env:VCC_WEB_PORT) { [int]$env:VCC_WEB_PORT } else { 5173 }
$apiBase = "http://127.0.0.1:$apiPort"
$webBase = "http://127.0.0.1:$webPort"

$stateDir = if ($env:VCC_LAUNCHER_STATE_DIR) { $env:VCC_LAUNCHER_STATE_DIR } else {
  Join-Path $env:LOCALAPPDATA 'VerifiedCruiseControl'
}
$logDir = Join-Path $stateDir 'logs'
$stateFile = Join-Path $stateDir 'services.json'

$venvPython = Join-Path $root '.venv/Scripts/python.exe'
$motherDir = Join-Path $root 'apps/mother'
$viteBin = Join-Path $motherDir 'node_modules/vite/bin/vite.js'
$extensionDir = Join-Path $root 'apps/extension/.output/chrome-mv3'

function Write-Step($text) { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }
function Write-Ok($text) { Write-Host "  [ok] $text" -ForegroundColor Green }
function Write-Warn($text) { Write-Host "  [!!] $text" -ForegroundColor Yellow }
function Write-Fail($text) { Write-Host "  [FAIL] $text" -ForegroundColor Red }

# Existing repo scripts manage their own errors and exit codes, but they
# assume the default Continue preference: tools like `uv` write progress
# to stderr, which a Stop preference would promote to a fatal error.
# Run them (and other native commands whose stderr is informational) with
# the preference relaxed, then judge by exit code.
function Invoke-Child($scriptPath) {
  $old = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    # Re-emit child stdout through the information stream so it stays
    # visible but cannot pollute this function's return value (which
    # must be exactly one exit code).
    & $scriptPath | ForEach-Object { Write-Host $_ }
    return $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $old
  }
}

# Minimal .env reader (stdlib only): KEY=value lines, # comments and
# blanks skipped, single/double quotes stripped. backend/.env is the
# documented config path (see backend/.env.example), but Django reads raw
# os.environ and nothing loads the file -- so the launcher injects it
# into the backend/worker environments it starts. Ambient process
# variables always win, which keeps test overrides (VCC_API_PORT, ...)
# and real user environment working.
function Get-DotEnv($path) {
  $vars = @{}
  if (-not (Test-Path $path)) { return $vars }
  foreach ($line in (Get-Content $path)) {
    $t = $line.Trim()
    if ($t -eq '' -or $t.StartsWith('#')) { continue }
    if ($t.StartsWith('export ')) { $t = $t.Substring(7).Trim() }
    $eq = $t.IndexOf('=')
    if ($eq -le 0) { continue }
    $key = $t.Substring(0, $eq).Trim()
    $val = $t.Substring($eq + 1).Trim()
    if ($val.Length -ge 2 -and (($val.StartsWith('"') -and $val.EndsWith('"')) -or ($val.StartsWith("'") -and $val.EndsWith("'")))) {
      $val = $val.Substring(1, $val.Length - 2)
    }
    if ($key -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { continue }
    if ($val -eq '') { continue }
    $vars[$key] = $val
  }
  return $vars
}

function Get-Services {
  if (-not (Test-Path $stateFile)) { return @() }
  try {
    $data = Get-Content $stateFile -Raw | ConvertFrom-Json
    if ($data -is [array]) { return $data }
    return @($data)
  } catch {
    Write-Warn "State file is unreadable; ignoring it ($stateFile)."
    return @()
  }
}

function Save-Services($services) {
  if (-not (Test-Path $stateDir)) { New-Item -ItemType Directory $stateDir | Out-Null }
  $array = @($services)
  if ($array.Count -eq 0) {
    if (Test-Path $stateFile) { Remove-Item $stateFile -Force }
    return
  }
  $array | ConvertTo-Json -Depth 4 | Set-Content $stateFile -Encoding UTF8
}

function Get-ProcessInfo($procId) {
  try {
    return Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction Stop
  } catch {
    return $null
  }
}

# A tracked service is ours only when the PID is alive, the command line
# names this repo (backend/worker run from its venv, Vite from its
# mother-app path), the marker matches, and the start time is close to
# what we recorded (guards against PID reuse).
function Test-OwnedService($svc) {
  $proc = Get-ProcessInfo $svc.pid
  if ($null -eq $proc) { return $false }
  $cmd = "$($proc.CommandLine)".Replace('/', '\')
  $marker = "$($svc.marker)".Replace('/', '\')
  $repo = "$root".Replace('/', '\')
  if ($cmd -notmatch [regex]::Escape($marker)) { return $false }
  if ($cmd -notmatch [regex]::Escape($repo)) { return $false }
  try {
    $started = ([datetime]$svc.startedUtc).ToUniversalTime()
    # Get-CimInstance already converts DMTF timestamps to DateTime; only
    # convert when the value arrives as a raw string (e.g. mocked data).
    $rawCreated = $proc.CreationDate
    if ($rawCreated -is [datetime]) {
      $created = $rawCreated.ToUniversalTime()
    } else {
      $created = [Management.ManagementDateTimeConverter]::ToDateTime("$rawCreated").ToUniversalTime()
    }
    # Generous window: PID + executable + exact command line must already
    # match, so this only guards wild PID reuse. Too tight a window would
    # orphan services after any crash/restart cycle longer than itself.
    if ([math]::Abs(($created - $started).TotalHours) -gt 12) { return $false }
  } catch {
    return $false
  }
  return $true
}

function Get-PortOwner($port) {
  try {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop |
      Select-Object -First 1
  } catch {
    return $null
  }
  $owner = Get-ProcessInfo $conn.OwningProcess
  return [pscustomobject]@{
    Pid = $conn.OwningProcess
    Name = if ($owner) { $owner.Name } else { '(unknown)' }
    CommandLine = if ($owner) { "$($owner.CommandLine)" } else { '' }
  }
}

function Start-TrackedService($name, $exe, $arguments, $workDir, $extraEnv) {
  if (-not (Test-Path $logDir)) { New-Item -ItemType Directory $logDir | Out-Null }
  $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
  $outLog = Join-Path $logDir "$name-$stamp-out.log"
  $errLog = Join-Path $logDir "$name-$stamp-err.log"
  $envBackup = @{}
  foreach ($key in $extraEnv.Keys) {
    $envBackup[$key] = [System.Environment]::GetEnvironmentVariable($key, 'Process')
    [System.Environment]::SetEnvironmentVariable($key, $extraEnv[$key], 'Process')
  }
  try {
    $proc = Start-Process -FilePath $exe -ArgumentList $arguments `
      -WorkingDirectory $workDir -WindowStyle Minimized `
      -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
  } finally {
    foreach ($key in $envBackup.Keys) {
      [System.Environment]::SetEnvironmentVariable($key, $envBackup[$key], 'Process')
    }
  }
  return [pscustomobject]@{
    name = $name
    pid = $proc.Id
    startedUtc = (Get-Date).ToUniversalTime().ToString('o')
    marker = $arguments[-1]
    exe = $exe
    outLog = $outLog
    errLog = $errLog
  }
}

function Wait-HttpOk($url, $timeoutSec, $label) {
  $waitSec = if ($env:VCC_LAUNCHER_WAIT_SEC) { [int]$env:VCC_LAUNCHER_WAIT_SEC } else { $timeoutSec }
  $deadline = (Get-Date).AddSeconds($waitSec)
  while ((Get-Date) -lt $deadline) {
    try {
      $res = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
      if ($res.StatusCode -ge 200 -and $res.StatusCode -lt 500) { return $true }
    } catch {
      Start-Sleep -Seconds 2
    }
  }
  Write-Fail "$label did not respond at $url within ${waitSec}s."
  return $false
}

function Get-SetupState {
  # Returns 'ready' (backend up + operator initialized), 'needs-setup'
  # (backend up, operator not initialized), or 'backend-down'.
  try {
    $status = Invoke-RestMethod -Uri "$apiBase/api/setup/status" -TimeoutSec 10
  } catch {
    return 'backend-down'
  }
  if ($status.initialized) { return 'ready' }
  return 'needs-setup'
}

function Show-SetupToken {
  # Prints the one-time operator setup token when setup is incomplete.
  # Read-only unless no token exists yet (same as scripts/start-backend).
  # Never written to any log file -- console only, for James to type in.
  # Never display raw command output here: on failure Django's stderr
  # arrives as fully-formatted error records, which is the wall of red
  # text this used to print. Classify instead and speak plainly.
  $oldPref = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $out = & $venvPython backend/manage.py setup_token 2>&1 | Out-String
    $code = $LASTEXITCODE
  } catch {
    $code = 1
    $out = ''
  } finally {
    $ErrorActionPreference = $oldPref
  }
  if ($code -ne 0) {
    # A token was minted by an earlier run, missed, and now cannot be
    # re-displayed (the server stores a hash, not the token). This is
    # the normal state of re-running Start before finishing setup.
    Write-Warn 'A setup token was already issued in an earlier run and cannot be shown again.'
    Write-Host '  If you saved it, enter it on the mother-app setup page now.'
    Write-Host '  Otherwise mint a fresh one (this invalidates the lost one) and re-run Start:'
    Write-Host '    .venv/Scripts/python backend/manage.py setup_token --regenerate'
    return
  }
  if ($out -match 'already initialized') {
    Write-Ok 'Operator setup already complete.'
    return
  }
  Write-Host ''
  Write-Host '  FIRST-TIME SETUP -- one-time operator token:' -ForegroundColor Yellow
  Write-Host ''
  Write-Host $out.Trim()
  Write-Host ''
  Write-Host "  1. Open $webBase in your browser (opened automatically)."
  Write-Host '  2. On the setup page, enter the token above.'
  Write-Host '  3. Copy the extension pairing token it shows (once) into the'
  Write-Host '     Verified Cruise Control popup in Chrome.'
}

function Show-InferenceStatus($serviceEnv) {
  # Effective config: ambient environment first, backend/.env as fallback.
  # Presence only -- values are never printed.
  $flag = $env:VCC_INFERENCE_ENABLED
  if ([string]::IsNullOrEmpty($flag) -and $serviceEnv.ContainsKey('VCC_INFERENCE_ENABLED')) {
    $flag = $serviceEnv['VCC_INFERENCE_ENABLED']
  }
  $enabled = $flag -eq '1'
  $hasBaseten = -not [string]::IsNullOrEmpty($env:BASETEN_API_KEY) -or $serviceEnv.ContainsKey('BASETEN_API_KEY')
  $hasMeta = -not [string]::IsNullOrEmpty($env:META_API_KEY) -or $serviceEnv.ContainsKey('META_API_KEY')
  Write-Host ''
  if ($enabled -and $hasBaseten -and $hasMeta) {
    Write-Host '  Inference: ENABLED (VCC_INFERENCE_ENABLED=1 with provider keys present).'
    Write-Host '  ACC audits you queue will spend real credits within server caps.'
  } else {
    Write-Host '  Inference: DISABLED (default; paid calls stay off).' -ForegroundColor Yellow
    Write-Host '  Queued ACC audits will pause with "Inference is disabled". To enable:'
    Write-Host '  set VCC_INFERENCE_ENABLED=1 plus BASETEN_API_KEY and META_API_KEY'
    Write-Host '  (see backend/.env.example), restart with Stop + Start, and authorize'
    Write-Host '  each run yourself. Startup succeeding does NOT mean live inference works.'
  }
}

# ---------------------------------------------------------------- Install

function Invoke-Install {
  Write-Step 'Checking prerequisites'
  $py = Get-Command python -ErrorAction SilentlyContinue
  if (-not $py) {
    Write-Fail 'Python not found. Install Python 3.13+ from https://www.python.org/downloads/'
    Write-Host '  (tick "Add python.exe to PATH" during setup), then re-run this installer.'
    return 1
  }
  $pyVersion = (python --version 2>&1) -replace 'Python ', ''
  $pyOk = $false
  try { $pyOk = ([version]$pyVersion.Trim() -ge [version]'3.13') } catch { $pyOk = $false }
  if (-not $pyOk) {
    Write-Fail "Python $pyVersion is too old -- 3.13+ is required: https://www.python.org/downloads/"
    return 1
  }
  Write-Ok "Python $pyVersion"
  $node = Get-Command node -ErrorAction SilentlyContinue
  if (-not $node) {
    Write-Fail 'Node.js not found. Install Node.js 20+ from https://nodejs.org/ (LTS), then re-run.'
    return 1
  }
  $nodeVersion = (node --version 2>&1).TrimStart('v')
  $nodeOk = $false
  try { $nodeOk = ([version]$nodeVersion -ge [version]'20.0.0') } catch { $nodeOk = $false }
  if (-not $nodeOk) {
    Write-Fail "Node.js $nodeVersion is too old -- 20+ is required: https://nodejs.org/"
    return 1
  }
  Write-Ok "Node.js v$nodeVersion"

  Write-Step 'Installing dependencies + migrating (scripts/setup.ps1)'
  Write-Host '  This converges safely: existing venvs, packages, and the'
  Write-Host '  database are kept; only missing pieces are added.'
  Set-Location $root
  if ((Invoke-Child (Join-Path $root 'scripts/setup.ps1')) -ne 0) { return 1 }

  Write-Step 'Building the Chrome extension'
  if ((Invoke-Child (Join-Path $root 'scripts/build-extension.ps1')) -ne 0) { return 1 }

  Write-Step 'Verifying extension output'
  $required = @('manifest.json', 'background.js', 'popup.html')
  foreach ($file in $required) {
    $path = Join-Path $extensionDir $file
    if (-not (Test-Path $path)) {
      Write-Fail "Missing expected extension file: $path"
      return 1
    }
    Write-Ok $file
  }
  $csDir = Join-Path $extensionDir 'content-scripts'
  if (-not (Test-Path $csDir)) {
    Write-Fail "Missing content-scripts directory: $csDir"
    return 1
  }
  Write-Ok 'content-scripts/'

  Write-Step 'Install Chrome extension (one-time, kept across restarts)'
  Write-Host "  Extension folder:  $extensionDir"
  Write-Host '  1. Visit chrome://extensions'
  Write-Host '  2. Enable Developer mode (top right).'
  Write-Host '  3. Choose Load unpacked and select the folder above.'
  Write-Host '  4. Pin Verified Cruise Control to the toolbar.'
  Write-Host '  Rebuilding later never deletes pairing or permissions; a separate'
  Write-Host '  rebuild only replaces these files, then use the reload button in'
  Write-Host '  chrome://extensions.'
  if (-not $NonInteractive) {
    $answer = Read-Host '  Open the extension folder in Explorer now? [Y/n]'
    if ($answer -eq '' -or $answer -match '^[Yy]') {
      explorer.exe $extensionDir
    }
  }

  Write-Step 'What happens next'
  Write-Host '  Double-click "Start Verified Cruise Control.cmd". On first run it'
  Write-Host '  shows a one-time operator token: enter it at the mother-app setup'
  Write-Host "  page ($webBase), copy the pairing token into the extension popup,"
  Write-Host '  and you are done -- later starts skip pairing entirely.'
  Write-Host ''
  Write-Host 'Setup complete.'
  return 0
}

# ----------------------------------------------------------------- Start

function Invoke-Start {
  Write-Step 'Verifying previous setup'
  $missing = @()
  if (-not (Test-Path $venvPython)) { $missing += '.venv (Python environment)' }
  if (-not (Test-Path (Join-Path $motherDir 'node_modules'))) { $missing += 'apps/mother/node_modules' }
  if (-not (Test-Path (Join-Path $extensionDir 'manifest.json'))) { $missing += 'built extension (.output/chrome-mv3)' }
  if ($missing.Count -gt 0) {
    Write-Fail 'Setup is incomplete -- missing:'
    foreach ($m in $missing) { Write-Host "    - $m" }
    Write-Host '  Run "Install Verified Cruise Control.cmd" first.'
    return 1
  }
  Write-Ok 'setup markers present'

  # backend/.env fills gaps only: anything already in this process's
  # environment (user exports, test overrides) takes precedence.
  # VCC_LAUNCHER_DOTENV points at another file for integration tests.
  $dotEnvPath = if ($env:VCC_LAUNCHER_DOTENV) { $env:VCC_LAUNCHER_DOTENV } else { Join-Path $root 'backend/.env' }
  $dotEnv = Get-DotEnv $dotEnvPath
  $serviceEnv = @{}
  foreach ($key in $dotEnv.Keys) {
    if ([string]::IsNullOrEmpty([System.Environment]::GetEnvironmentVariable($key, 'Process'))) {
      $serviceEnv[$key] = $dotEnv[$key]
    }
  }
  if ($dotEnv.Count -gt 0) {
    Write-Ok "backend/.env supplies $($serviceEnv.Count) variable(s) (ambient env wins; values never shown)"
  }

  $services = @(Get-Services)
  $live = @()
  foreach ($svc in $services) {
    if (Test-OwnedService $svc) {
      $live += $svc
    } else {
      Write-Warn "Dropping stale entry: $($svc.name) (PID $($svc.pid) is gone or not ours)."
    }
  }

  $needBackend = ($live | Where-Object { $_.name -eq 'backend' }).Count -eq 0
  $needWorker = ($live | Where-Object { $_.name -eq 'worker' }).Count -eq 0
  $needMother = ($live | Where-Object { $_.name -eq 'mother' }).Count -eq 0

  # Occupied ports belong to someone: identify, never kill.
  $checks = @(
    @{ name = 'backend'; port = $apiPort; need = $needBackend },
    @{ name = 'mother'; port = $webPort; need = $needMother }
  )
  foreach ($check in $checks) {
    if (-not $check.need) { continue }
    $owner = Get-PortOwner $check.port
    if ($owner -ne $null) {
      Write-Fail "Port $($check.port) ($($check.name)) is already in use by PID $($owner.Pid) ($($owner.Name))."
      Write-Host '  The launcher never stops processes it did not start.'
      if ($owner.CommandLine -match [regex]::Escape($root)) {
        Write-Host '  That process looks like this project but was started outside the'
        Write-Host '  launcher (e.g. scripts/dev.ps1). Close its window or run'
        Write-Host '  ./scripts/stop-dev.ps1, then re-run Start.'
      } else {
        Write-Host '  Stop or reconfigure the owning application yourself, then re-run Start.'
        Write-Host "  Command line: $($owner.CommandLine)"
      }
      return 1
    }
  }

  if ($needBackend) {
    Write-Step "Starting Django backend ($apiBase)"
    $svc = Start-TrackedService 'backend' $venvPython `
      @('backend/manage.py', 'runserver', "127.0.0.1:$apiPort", '--noreload') $root $serviceEnv
    # Marker + repo-path check together prove launcher ownership on Stop.
    $svc.marker = 'manage.py runserver'
    $live += $svc
    Write-Ok "backend PID $($svc.pid) (log: $($svc.outLog))"
  } else {
    Write-Ok 'backend already running (owned by launcher)'
  }

  if ($needWorker) {
    Write-Step 'Starting audit worker'
    $svc = Start-TrackedService 'worker' $venvPython `
      @('backend/manage.py', 'run_audit_worker') $root $serviceEnv
    $svc.marker = 'manage.py run_audit_worker'
    $live += $svc
    Write-Ok "worker PID $($svc.pid) (log: $($svc.outLog))"
  } else {
    Write-Ok 'audit worker already running (owned by launcher)'
  }

  if ($needMother) {
    Write-Step "Starting mother app ($webBase)"
    $svc = Start-TrackedService 'mother' 'node' `
      @($viteBin, '--host', '127.0.0.1', '--port', "$webPort", '--strictPort') $motherDir `
      @{ VCC_API_PROXY = $apiBase }
    $svc.marker = 'vite/bin/vite.js'
    $live += $svc
    Write-Ok "mother PID $($svc.pid) (log: $($svc.outLog))"
  } else {
    Write-Ok 'mother app already running (owned by launcher)'
  }
  Save-Services $live

  Write-Step 'Waiting for services'
  if (-not (Wait-HttpOk "$apiBase/api/health" 90 'Backend')) { return 1 }
  Write-Ok "backend ready at $apiBase"
  $workerAlive = $true
  foreach ($svc in ($live | Where-Object { $_.name -eq 'worker' })) {
    if (-not (Test-OwnedService $svc)) { $workerAlive = $false }
  }
  if (-not $workerAlive) {
    Write-Fail 'Audit worker exited during startup. See its log under:'
    Write-Host "  $logDir"
    return 1
  }
  Write-Ok 'audit worker alive'
  if (-not (Wait-HttpOk $webBase 90 'Mother app')) { return 1 }
  Write-Ok "mother app ready at $webBase"

  $attentionNeeded = $false
  $setupState = Get-SetupState
  if ($setupState -eq 'needs-setup') {
    Write-Warn 'Operator setup is not complete yet.'
    Show-SetupToken
    $attentionNeeded = $true
  } elseif ($setupState -eq 'ready') {
    Write-Ok 'operator setup complete (pairing persists; no action needed)'
  } else {
    Write-Warn 'Could not reach the backend setup endpoint; continuing anyway.'
  }

  Show-InferenceStatus $serviceEnv

  if (-not $env:VCC_NO_BROWSER) {
    Write-Step 'Opening the mother app'
    Start-Process $webBase | Out-Null
    Write-Ok 'browser opened (after both servers were ready)'
  }

  Write-Host ''
  if ($attentionNeeded) {
    Write-Host 'Started with one attention item above (first-time setup token).' -ForegroundColor Yellow
    return 2
  }
  Write-Host 'All services running.' -ForegroundColor Green
  return 0
}

# ------------------------------------------------------------------ Stop

function Invoke-Stop {
  $services = @(Get-Services)
  if ($services.Count -eq 0) {
    Write-Host 'No launcher-tracked services. Nothing to stop.'
    Write-Host '(Processes from scripts/dev.ps1 are stopped with ./scripts/stop-dev.ps1.)'
    return 0
  }
  $remaining = @()
  foreach ($svc in $services) {
    if (Test-OwnedService $svc) {
      try {
        Stop-Process -Id $svc.pid -Force
        Write-Ok "stopped $($svc.name) (PID $($svc.pid))"
      } catch {
        Write-Warn "could not stop $($svc.name) (PID $($svc.pid)): $_"
        $remaining += $svc
      }
    } else {
      $proc = Get-ProcessInfo $svc.pid
      if ($null -eq $proc) {
        Write-Ok "$($svc.name) (PID $($svc.pid)) already exited"
      } else {
        Write-Warn "skipping PID $($svc.pid): not started by this launcher -- left running"
        $remaining += $svc
      }
    }
  }
  Save-Services $remaining
  if ($remaining.Count -eq 0) {
    Write-Host 'Shutdown complete. Startup state cleaned up.'
  } else {
    Write-Warn 'Some entries remain tracked; re-run Stop after resolving them.'
  }
  return 0
}

# ------------------------------------------------------------------ main

switch ($Action) {
  'Install' { exit (Invoke-Install) }
  'Start' { exit (Invoke-Start) }
  'Stop' { exit (Invoke-Stop) }
}



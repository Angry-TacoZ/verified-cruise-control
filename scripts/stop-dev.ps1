# Stop dev processes started by scripts/dev.ps1.
# Matches only our own command lines; never kills by port alone.
$ErrorActionPreference = 'Stop'

$patterns = @('manage\.py runserver', 'manage\.py run_audit_worker', 'fixtures.serve', 'vite.*--port 5173|vite preview')
$killed = 0
foreach ($proc in Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='node.exe'") {
  $cmd = $proc.CommandLine
  if ($null -eq $cmd) { continue }
  foreach ($pattern in $patterns) {
    if ($cmd -match $pattern) {
      Write-Host "Stopping $($proc.ProcessId): $cmd"
      Stop-Process -Id $proc.ProcessId -Force
      $killed += 1
      break
    }
  }
}
Write-Host "Stopped $killed process(es)."

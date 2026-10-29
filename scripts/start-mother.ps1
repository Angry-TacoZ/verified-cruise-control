# Start the Svelte mother app on http://127.0.0.1:5173 (foreground).
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location (Join-Path $root 'apps/mother')

npm run dev

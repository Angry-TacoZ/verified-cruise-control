# Build the WXT extension and print the unpacked output directory.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location (Join-Path $root 'apps/extension')

npm run build
Write-Host ''
Write-Host "Load unpacked from:  $(Join-Path (Get-Location) '.output/chrome-mv3')"

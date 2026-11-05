@echo off
REM Verified Cruise Control -- safe shutdown.
REM Stops only services started by the Start launcher. Unrelated apps,
REM and processes from scripts\dev.ps1, are never touched.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launcher.ps1" -Action Stop
pause

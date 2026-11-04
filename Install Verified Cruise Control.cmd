@echo off
REM Verified Cruise Control -- one-time setup.
REM Checks prerequisites, installs dependencies, builds the extension,
REM and walks through loading it in Chrome. No admin rights needed.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launcher.ps1" -Action Install
if errorlevel 1 (
  echo.
  echo Setup did not complete. Fix the messages above, then double-click again.
  pause
) else (
  pause
)

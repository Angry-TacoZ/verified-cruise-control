@echo off
REM Verified Cruise Control -- daily launcher.
REM Starts backend + worker + mother app (once each), waits until ready,
REM opens the dashboard. Window stays open only when attention is needed.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launcher.ps1" -Action Start
if errorlevel 1 (
  echo.
  echo Startup needs attention - see the messages above.
  pause
)

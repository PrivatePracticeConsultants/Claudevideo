@echo off
rem Launches the Medicare Order & Referring Tracker.
rem Uses PowerShell 7 (pwsh) when installed, otherwise built-in Windows PowerShell.
setlocal
cd /d "%~dp0"
where pwsh >nul 2>nul
if %errorlevel%==0 (
  pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-OrderReferringTracker.ps1"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-OrderReferringTracker.ps1"
)
if %errorlevel% neq 0 pause

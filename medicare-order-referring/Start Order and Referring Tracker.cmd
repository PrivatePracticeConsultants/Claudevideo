@echo off
rem Launches the Medicare Order & Referring Tracker.
rem Uses PowerShell 7 (pwsh) when installed, otherwise built-in Windows PowerShell.
rem Note: "if errorlevel N" is used instead of "%errorlevel%" because the latter
rem is expanded at parse time inside ( ) blocks and would read a stale value.
setlocal
cd /d "%~dp0"

where pwsh >nul 2>nul
if errorlevel 1 goto winps

pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-OrderReferringTracker.ps1"
if not errorlevel 1 goto done
echo Retrying with the built-in Windows PowerShell...

:winps
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-OrderReferringTracker.ps1"

:done
if errorlevel 1 pause

@echo off
REM Double-click this to run MRF Explorer with automatic restart.
REM If the app ever stops unexpectedly (a rare native crash under memory
REM pressure, a power blip), it relaunches on its own and resumes exactly
REM where it left off. To stop it for good, close this window or press Ctrl-C.
title MRF Explorer (auto-restart)
cd /d "%~dp0"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
mrfx serve --supervise
echo.
echo MRF Explorer has stopped. You can close this window.
pause

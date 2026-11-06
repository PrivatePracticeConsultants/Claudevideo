@echo off
REM Double-click this to run MRF Explorer with automatic restart.
REM If the app ever stops unexpectedly (a rare native crash under memory
REM pressure, a power blip), it relaunches on its own and resumes exactly
REM where it left off. To stop it for good, close this window or press Ctrl-C.
REM
REM It runs THIS folder's program with THIS folder's Python environment
REM (".venv\Scripts\python.exe -m mrfx"), never the "mrfx" command: that
REM command remembers the folder it was installed in, so after the folder was
REM renamed or moved it failed with "No module named 'mrfx'". This works
REM wherever the folder lives.
title MRF Explorer (auto-restart)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto noenv
".venv\Scripts\python.exe" -m mrfx serve --supervise
echo.
echo MRF Explorer has stopped. You can close this window.
pause
exit /b
:noenv
echo.
echo The program's Python environment - the .venv folder - is not in this folder:
echo   %~dp0
echo Set it up once with the commands in HOW_TO_RUN.md, Part 1 Step 4,
echo then double-click this file again. Your data is not affected.
echo.
pause
exit /b 1

@echo off
REM Drag the new mrfx_project.zip onto this file to update MRF Explorer.
REM Your data (the data folder) and your settings (config\mrfx.yaml) are kept.
REM Everything runs inside ONE parenthesized block: cmd.exe reads the whole
REM block before running it, so the update may safely replace this file.
REM It runs this folder's own Python environment (".venv\Scripts\python.exe
REM -m mrfx"), so it works even after the folder was renamed or moved.
(
  title Update MRF Explorer
  cd /d "%~dp0"
  if "%~1"=="" (
    echo.
    echo To update: close MRF Explorer, then DRAG the new mrfx_project.zip file
    echo onto this "Update MRF Explorer" icon.
    echo Your data and settings are kept.
  ) else if not exist ".venv\Scripts\python.exe" (
    echo.
    echo The program's Python environment - the .venv folder - is not in this folder.
    echo Set it up once with HOW_TO_RUN.md, Part 1 Step 4, then try again.
  ) else (
    ".venv\Scripts\python.exe" -m mrfx update "%~1"
  )
  echo.
  pause
  exit /b
)

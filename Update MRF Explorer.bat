@echo off
REM Drag the new mrfx_project.zip onto this file to update MRF Explorer.
REM Your data (the data folder) and your settings (config\mrfx.yaml) are kept.
REM Everything runs inside ONE parenthesized block: cmd.exe reads the whole
REM block before running it, so the update may safely replace this file.
(
  title Update MRF Explorer
  cd /d "%~dp0"
  if "%~1"=="" (
    echo.
    echo To update: close MRF Explorer, then DRAG the new mrfx_project.zip file
    echo onto this "Update MRF Explorer" icon.
    echo Your data and settings are kept.
  ) else (
    if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
    mrfx update "%~1"
  )
  echo.
  pause
  exit /b
)

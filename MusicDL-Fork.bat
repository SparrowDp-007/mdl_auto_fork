@echo off
cd /d "%~dp0"
py -X utf8 launcher.py %*
if errorlevel 1 (
  echo.
  echo Failed. Press any key to exit...
  pause >nul
)

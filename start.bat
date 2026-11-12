@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\installed.ok" (
  echo First run: setting up Python environment...
  if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv 2>nul || python -m venv .venv
  )
  if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Python 3.10 or newer is required.
    echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH", then run this again.
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Package install failed.
    pause
    exit /b 1
  )
  echo ok> ".venv\installed.ok"
)

if /i "%~1"=="setup" exit /b 0

echo Starting Adhan Cast. Keep this window open, or run install-startup.bat to run it in the background.
".venv\Scripts\python.exe" app.py --open
pause

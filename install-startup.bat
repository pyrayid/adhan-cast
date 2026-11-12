@echo off
setlocal
cd /d "%~dp0"
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Adhan Cast.lnk"

if /i "%~1"=="remove" (
  if exist adhan.pid for /f %%p in (adhan.pid) do taskkill /PID %%p /F >nul 2>&1
  if exist "%LNK%" del "%LNK%"
  echo Adhan Cast stopped and removed from startup.
  pause
  exit /b 0
)

call "%~dp0start.bat" setup || exit /b 1

rem Stop any copy that is already running
if exist adhan.pid for /f %%p in (adhan.pid) do taskkill /PID %%p /F >nul 2>&1

powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK); $s.TargetPath='%~dp0.venv\Scripts\pythonw.exe'; $s.Arguments='\"%~dp0app.py\"'; $s.WorkingDirectory='%~dp0'; $s.Save()"
if errorlevel 1 (
  echo Could not create the startup shortcut.
  pause
  exit /b 1
)

start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0app.py"
timeout /t 4 >nul
start "" http://localhost:8080
echo.
echo Adhan Cast now runs in the background and starts every time you sign in.
echo Dashboard: http://localhost:8080   (bookmark it)
echo To stop and remove it: install-startup.bat remove
pause

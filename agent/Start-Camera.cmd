@echo off
cd /d "%~dp0"
py -3 --version >nul 2>&1
if errorlevel 1 (
  echo Install Python 3 from python.org with the Python launcher enabled, then open this file again.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 goto failed
)
.venv\Scripts\python.exe -c "import cv2, simple_websocket" >nul 2>&1
if errorlevel 1 (
  echo Installing camera components. This is only needed the first time.
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 goto failed
)
.venv\Scripts\python.exe setup_camera.py
if errorlevel 1 goto failed
exit /b 0
:failed
echo Setup could not finish. Review the message above and try again.
pause
exit /b 1

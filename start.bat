@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtualenv...
  python -m venv .venv || goto :err
  ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :err
)

".venv\Scripts\python.exe" dashboard.py %*
goto :eof

:err
echo.
echo Setup failed. Make sure Python 3.10+ is on PATH.
pause

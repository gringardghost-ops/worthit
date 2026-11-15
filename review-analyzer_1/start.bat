@echo off
setlocal
cd /d "%~dp0"
title worth it?

where python >nul 2>nul
if errorlevel 1 (
  echo ERROR: Python 3.11+ was not found on PATH. Install it from https://www.python.org/downloads/
  echo Tick "Add python.exe to PATH" in the installer.
  pause
  exit /b 1
)

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo ERROR: Python 3.11 or newer is required.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  python -m venv .venv
  if errorlevel 1 (
    echo ERROR: Could not create the virtual environment.
    pause
    exit /b 1
  )
)

call ".venv\Scripts\activate.bat"

echo Installing dependencies...
python -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo ERROR: Dependency installation failed. Check your internet connection.
  pause
  exit /b 1
)

if not exist ".env" (
  copy /y ".env.example" ".env" >nul
  echo.
  echo ==================================================================
  echo  WARNING: .env was just created from .env.example.
  echo  Open .env and paste your SERPAPI_KEY and GEMINI_API_KEY, then
  echo  restart this script. The app will start, but analyses will fail
  echo  until both keys are set.
  echo ==================================================================
  echo.
)

start "" /b powershell -NoProfile -Command "Start-Sleep -Seconds 2; Start-Process 'http://localhost:8000'"

echo Starting worth it? on http://localhost:8000  (Ctrl+C to stop)
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
pause

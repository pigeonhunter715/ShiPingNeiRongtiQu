@echo off
setlocal
cd /d "%~dp0"

echo.
echo ========================================
echo  Bilibili Video Content Search
echo ========================================
echo.

set "PYTHON_CMD=python"
where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if errorlevel 1 (
    echo Python was not found.
    echo Please install Python 3.11 or newer from https://www.python.org/downloads/
    echo Make sure to check "Add python.exe to PATH" during installation.
    echo.
    pause
    exit /b 1
  )
  set "PYTHON_CMD=py -3"
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating local virtual environment...
  %PYTHON_CMD% -m venv .venv
  if errorlevel 1 (
    echo Failed to create virtual environment.
    pause
    exit /b 1
  )
)

call ".venv\Scripts\activate.bat"

echo Installing Python dependencies. First run can take a while...
python -m pip install --upgrade pip
if errorlevel 1 (
  echo Failed to upgrade pip.
  pause
  exit /b 1
)

pip install -r requirements.txt
if errorlevel 1 (
  echo Failed to install dependencies.
  echo If the network is slow, run start.bat again later.
  pause
  exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo.
  echo WARNING: ffmpeg was not found.
  echo Import and search can still work, but transcription needs ffmpeg.
  echo You can run install_ffmpeg.bat, or install it with:
  echo winget install Gyan.FFmpeg
  echo.
)

echo Starting local web app...
echo Open http://127.0.0.1:8000 if the browser does not open automatically.
start "" "http://127.0.0.1:8000"
echo.
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

echo.
echo Server stopped.
pause

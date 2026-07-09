@echo off
setlocal

echo Installing ffmpeg with winget...
where winget >nul 2>nul
if errorlevel 1 (
  echo winget was not found.
  echo Please install ffmpeg manually from https://ffmpeg.org/download.html
  pause
  exit /b 1
)

winget install Gyan.FFmpeg
echo.
echo If ffmpeg was installed successfully, close and reopen start.bat.
pause

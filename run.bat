@echo off
if not exist ".venv\Scripts\pythonw.exe" (
    echo ERROR: Virtual environment not found. Run setup.bat first.
    pause
    exit /b 1
)
cd /d "%~dp0src"
echo Starting Tachyon Transcripts...
start "" "..\.venv\Scripts\pythonw" -m tachyon.main
echo Tachyon Transcripts is running in the system tray.
timeout /t 10 >nul

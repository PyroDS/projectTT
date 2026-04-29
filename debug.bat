@echo off
cd /d "%~dp0src"
echo Starting Tachyon Transcripts (debug mode)...
echo Console output will appear below. Close this window to stop.
echo.
"..\.venv\Scripts\python" -m tachyon.main
pause

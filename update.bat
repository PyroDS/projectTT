@echo off
echo ============================================
echo   Tachyon Transcripts - Update
echo ============================================
echo.

if not exist ".venv\Scripts\pip.exe" (
    echo ERROR: Virtual environment not found. Run setup.bat first.
    pause
    exit /b 1
)

echo [1/2] Updating pinned dependencies (torch, torchaudio)...
.venv\Scripts\pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cpu
if errorlevel 1 (
    echo ERROR: Failed to update torch. Check your internet connection.
    pause
    exit /b 1
)

echo [2/2] Installing/updating project dependencies...
.venv\Scripts\pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to update dependencies.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   Update complete! Run 'run.bat' to start.
echo ============================================
pause

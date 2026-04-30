@echo off
echo ============================================
echo   Tachyon Transcripts - First-Time Setup
echo ============================================
echo.

:: --- Find Python 3.11 ---
echo [1/6] Locating Python 3.11...
set "PY311_EXE="
set "PY311_ARGS="

:: Check common install locations
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%ProgramFiles%\Python311\python.exe"
    "%ProgramFiles(x86)%\Python311\python.exe"
    "C:\Python311\python.exe"
) do (
    if exist %%P (
        set "PY311_EXE=%%~P"
        goto :found_python
    )
)

:: Try py launcher as fallback
where py >nul 2>&1
if not errorlevel 1 (
    py -3.11 --version >nul 2>&1
    if not errorlevel 1 (
        set "PY311_EXE=py"
        set "PY311_ARGS=-3.11"
        goto :found_python
    )
)

echo ERROR: Python 3.11 not found.
echo.
echo Please install Python 3.11.9 from:
echo   https://www.python.org/downloads/release/python-3119/
echo.
echo Download "Windows installer (64-bit)" and run it.
echo You do NOT need to add it to PATH.
pause
exit /b 1

:found_python
if "%PY311_ARGS%"=="" (
    echo   Found: %PY311_EXE%
) else (
    echo   Found: %PY311_EXE% %PY311_ARGS%
)

:: --- Create venv ---
echo [2/6] Creating Python virtual environment...
if exist .venv (
    echo   Removing old virtual environment...
    rmdir /s /q .venv
)
"%PY311_EXE%" %PY311_ARGS% -m venv .venv
if errorlevel 1 (
    echo ERROR: Failed to create venv.
    pause
    exit /b 1
)

:: Verify Python version in venv
.venv\Scripts\python -c "import sys; v=sys.version_info; exit(0 if v.major==3 and v.minor==11 else 1)"
if errorlevel 1 (
    echo ERROR: Virtual environment has wrong Python version.
    .venv\Scripts\python --version
    echo Expected Python 3.11.x
    pause
    exit /b 1
)
echo   Confirmed: Python 3.11 virtual environment created.

:: --- Install dependencies ---
echo [3/6] Installing dependencies (this may take several minutes)...
.venv\Scripts\pip install --upgrade pip >nul 2>&1
.venv\Scripts\pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cpu
if errorlevel 1 (
    echo ERROR: Failed to install PyTorch.
    pause
    exit /b 1
)

:: Install webrtcvad-wheels BEFORE resemblyzer (ships pre-built, no C++ build tools needed)
:: Then install resemblyzer --no-deps so it doesn't pull in the original webrtcvad source package
.venv\Scripts\pip install "webrtcvad-wheels>=2.0.10"
.venv\Scripts\pip install --no-deps "resemblyzer>=0.1.3"

:: Prefer the pinned lock file (transitive closure pinned for supply-chain
:: safety). Fall back to the human-curated top-level list if the lock is
:: missing.
if exist requirements-lock.txt (
    echo   Using requirements-lock.txt ^(pinned^)...
    .venv\Scripts\pip install -r requirements-lock.txt
) else (
    echo   No lock file found, falling back to requirements.txt...
    .venv\Scripts\pip install -r requirements.txt
)
if errorlevel 1 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

:: --- Download models ---
echo [4/6] Downloading Whisper model for detected hardware (this may take a few minutes)...
.venv\Scripts\python scripts\download_model.py
if errorlevel 1 (
    echo WARNING: Model download failed. It will be downloaded on first run.
)

echo [5/6] Downloading speaker embedding model (speechbrain ECAPA-TDNN, pinned)...
.venv\Scripts\python -c "import sys; sys.path.insert(0, r'%~dp0src'); from tachyon.model_pins import SPEECHBRAIN_ECAPA_REPO, SPEECHBRAIN_ECAPA_REVISION; from speechbrain.inference.speaker import EncoderClassifier; EncoderClassifier.from_hparams(source=SPEECHBRAIN_ECAPA_REPO, savedir=r'%~dp0models\speechbrain-ecapa', revision=SPEECHBRAIN_ECAPA_REVISION)"
if errorlevel 1 (
    echo WARNING: Speaker model download failed. It will be downloaded on first use.
)

echo [6/6] Downloading lightweight speaker model (resemblyzer)...
.venv\Scripts\python -c "from resemblyzer import VoiceEncoder; VoiceEncoder()"
if errorlevel 1 (
    echo WARNING: Resemblyzer model download failed. It will be downloaded on first use.
)

echo.
echo ============================================
echo   Setup complete! Run 'run.bat' to start.
echo ============================================
pause

@echo off
:: ==========================================================================
::  Tachyon Transcripts — Installer Build Script
::
::  Runs PyInstaller and then Inno Setup to produce a shippable .exe.
::
::  Prerequisites:
::    1. A working .venv in the project root (run setup.bat first).
::    2. PyInstaller installed in the venv:
::         .venv\Scripts\pip install pyinstaller>=6.0
::    3. Inno Setup 6 installed; iscc.exe on PATH or in the default
::       "%ProgramFiles(x86)%\Inno Setup 6" location.
::
::  Usage (from project root):
::    installer\build_installer.bat
::
::  Output:
::    installer\dist\TachyonTranscripts-Setup-<version>.exe
:: ==========================================================================

setlocal enabledelayedexpansion

:: Move to project root (parent of this script's directory).
pushd "%~dp0.."

echo.
echo ============================================
echo   Tachyon Transcripts -- Installer Build
echo ============================================
echo.

:: --- Sanity checks --------------------------------------------------------

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv not found.  Run setup.bat first.
    popd
    exit /b 1
)

.venv\Scripts\python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo [1/5] Installing PyInstaller into the venv...
    .venv\Scripts\pip install "pyinstaller>=6.0"
    if errorlevel 1 (
        echo ERROR: Failed to install PyInstaller.
        popd
        exit /b 1
    )
)

:: --- Generate the Windows icon --------------------------------------------

echo [2/5] Generating Windows icon (assets\icon.ico)...
.venv\Scripts\python scripts\make_icon.py
if errorlevel 1 (
    echo WARNING: icon generation failed; installer will use the default.
)

:: --- Clean previous PyInstaller output ------------------------------------

echo [3/5] Cleaning previous PyInstaller build...
if exist build\TachyonTranscripts (
    rmdir /s /q build\TachyonTranscripts
)
if exist dist\TachyonTranscripts (
    rmdir /s /q dist\TachyonTranscripts
)

:: --- Run PyInstaller ------------------------------------------------------

echo [4/5] Running PyInstaller (this takes several minutes)...
.venv\Scripts\pyinstaller installer\tachyon.spec --noconfirm --clean
if errorlevel 1 (
    echo ERROR: PyInstaller build failed.
    popd
    exit /b 1
)

if not exist "dist\TachyonTranscripts\TachyonTranscripts.exe" (
    echo ERROR: PyInstaller did not produce the expected executable.
    popd
    exit /b 1
)

echo [4.5/5] Verifying bundled CUDA runtime DLLs...
set "CUDA_ROOT=dist\TachyonTranscripts\_internal\cuda"
for %%D in (cublas64_12.dll cudnn64_9.dll) do (
    if not exist "%CUDA_ROOT%\%%D" (
        echo ERROR: Missing required CUDA DLL: %CUDA_ROOT%\%%D
        echo Ensure .venv includes nvidia-cublas-cu12 and nvidia-cudnn-cu12.
        popd
        exit /b 1
    )
)

:: --- Locate Inno Setup compiler -------------------------------------------

echo [5/5] Running Inno Setup...

set "ISCC="
where iscc >nul 2>&1
if not errorlevel 1 (
    set "ISCC=iscc"
    goto :have_iscc
)

for %%P in (
    "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
    "%ProgramFiles%\Inno Setup 6\ISCC.exe"
    "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
) do (
    if exist %%P (
        set "ISCC=%%~P"
        goto :have_iscc
    )
)

echo ERROR: Inno Setup compiler (ISCC.exe) not found.
echo Install Inno Setup 6 from https://jrsoftware.org/isdl.php
popd
exit /b 1

:have_iscc
echo   Using Inno Setup: %ISCC%

"%ISCC%" installer\Tachyon.iss
if errorlevel 1 (
    echo ERROR: Inno Setup build failed.
    popd
    exit /b 1
)

echo.
echo ============================================
echo   BUILD COMPLETE
echo ============================================
echo.
echo Installer:
dir /b installer\dist\TachyonTranscripts-Setup-*.exe 2>nul
echo.
echo Test the installer on a clean Windows VM before distributing.
echo Remember: the installer is NOT code-signed; SmartScreen will warn.
echo.
popd
endlocal

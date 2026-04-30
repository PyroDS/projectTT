# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Tachyon Transcripts.

Build with:
    pyinstaller installer/tachyon.spec --noconfirm --clean

Produces:
    dist/TachyonTranscripts/              <- one-folder bundle
    dist/TachyonTranscripts/TachyonTranscripts.exe

The resulting folder is consumed by ``installer/Tachyon.iss``.

Implementation notes
--------------------
*   faster-whisper loads CTranslate2 at import time; CTranslate2 in turn
    looks for CUDA DLLs (cublas, cudnn) beside its own binary.  The
    ``nvidia-cublas-cu12`` and ``nvidia-cudnn-cu12`` pip packages install
    those DLLs into ``site-packages/nvidia/*/bin`` — which is **not** on
    the PATH PyInstaller synthesises at runtime.  We collect them
    explicitly and place them next to the frozen executable.

*   ``sounddevice`` ships its own ``libportaudio`` binary and
    ``PyAudioWPatch`` ships a patched ``portaudio_x64.dll`` — both are
    picked up by ``collect_dynamic_libs``.

*   ``speechbrain`` pulls in a mountain of torch/transformers code.  We
    use ``collect_all`` on it (and the diarizer backends that are
    always present) so hidden-submodule imports don't break at runtime.

*   The ``console=False`` mode is the equivalent of ``pythonw`` —
    launches without a terminal window, suitable for a tray app.
"""

from __future__ import annotations
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

# ---------------------------------------------------------------------------
# Project layout
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(SPECPATH).parent.resolve()
SRC_ROOT = PROJECT_ROOT / "src"
ENTRY = str(SRC_ROOT / "tachyon" / "main.py")
APP_NAME = "TachyonTranscripts"
CUDA_DLL_DEST = "cuda"
EXPECTED_CUDA_DLLS = ("cublas64_12.dll", "cudnn64_9.dll")


def _collect_cuda_binaries() -> list[tuple[str, str]]:
    """Collect CUDA DLLs from nvidia pip packages into a fixed destination.

    Using a stable destination keeps runtime DLL discovery deterministic in
    frozen builds (``_internal/cuda``).
    """
    collected: list[tuple[str, str]] = []
    seen_sources: set[str] = set()

    for nv_pkg in ("nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_runtime"):
        try:
            raw_binaries = collect_dynamic_libs(nv_pkg)
        except Exception:
            raw_binaries = []
        for src, _dest in raw_binaries:
            src_str = str(src)
            if src_str.lower() in seen_sources:
                continue
            seen_sources.add(src_str.lower())
            collected.append((src_str, CUDA_DLL_DEST))

    # Namespace-packaged nvidia wheels can be skipped by collect_dynamic_libs;
    # pick up DLLs directly from site-packages as a fallback.
    site_packages = (Path(sys.executable).parent / ".." / "Lib" / "site-packages").resolve()
    for pattern in ("nvidia/*/bin/*.dll", "nvidia/*/lib/*.dll"):
        for dll_path in site_packages.glob(pattern):
            src_str = str(dll_path)
            if src_str.lower() in seen_sources:
                continue
            seen_sources.add(src_str.lower())
            collected.append((src_str, CUDA_DLL_DEST))

    return collected


def _assert_expected_cuda_dlls(cuda_binaries: list[tuple[str, str]]) -> None:
    """Fail the build early when required CUDA DLLs are missing."""
    found = {Path(src).name.lower() for src, _ in cuda_binaries}
    missing = [name for name in EXPECTED_CUDA_DLLS if name.lower() not in found]
    if not missing:
        return

    missing_csv = ", ".join(missing)
    raise RuntimeError(
        "Missing required CUDA DLL(s) in PyInstaller inputs: "
        f"{missing_csv}. Ensure the build venv has "
        "'nvidia-cublas-cu12' and 'nvidia-cudnn-cu12' installed."
    )

# ---------------------------------------------------------------------------
# Hidden imports — modules that PyInstaller's static analysis misses
# ---------------------------------------------------------------------------

hidden_imports: list[str] = []
hidden_imports += collect_submodules("tachyon")
hidden_imports += collect_submodules("faster_whisper")
hidden_imports += collect_submodules("ctranslate2")

# Speaker diarization backends — always include speechbrain + resemblyzer;
# pyannote is optional and left out to keep the installer smaller.
for pkg in ("speechbrain", "resemblyzer", "sklearn"):
    try:
        hidden_imports += collect_submodules(pkg)
    except Exception:
        pass

# Hardware-detection paths — tried in order at runtime.
hidden_imports += ["pynvml", "torch", "torch.cuda"]

# Misc: pystray's Win32 backend resolves dynamically.
hidden_imports += ["pystray._win32"]

# ---------------------------------------------------------------------------
# Data files + dynamic libraries
# ---------------------------------------------------------------------------

datas: list[tuple[str, str]] = []
binaries: list[tuple[str, str]] = []

# faster-whisper / CTranslate2: collect everything (model assets + DLLs)
for pkg in ("faster_whisper", "ctranslate2"):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
        datas += pkg_datas
        binaries += pkg_binaries
        hidden_imports += pkg_hidden
    except Exception:
        pass

# CUDA runtime DLLs from the nvidia pip packages.  CTranslate2 on Windows
# dlopens ``cublas64_12.dll`` / ``cudnn64_9.dll`` with bare filenames, so
# they must be discoverable at runtime from a known location.
cuda_binaries = _collect_cuda_binaries()
_assert_expected_cuda_dlls(cuda_binaries)
binaries += cuda_binaries

# Audio backends: portaudio ships inside these wheels
for audio_pkg in ("sounddevice", "pyaudiowpatch", "soundfile"):
    try:
        binaries += collect_dynamic_libs(audio_pkg)
        datas += collect_data_files(audio_pkg)
    except Exception:
        pass

# speechbrain ships yaml hparams files + small lookup tables
try:
    datas += collect_data_files("speechbrain")
except Exception:
    pass

# Resemblyzer ships pretrained weights inside its wheel
try:
    datas += collect_data_files("resemblyzer")
except Exception:
    pass

# Application assets (icon etc.)
assets_dir = PROJECT_ROOT / "assets"
if assets_dir.is_dir():
    for f in assets_dir.iterdir():
        if f.is_file():
            datas.append((str(f), "assets"))

# ---------------------------------------------------------------------------
# Excludes — trim obvious fat
# ---------------------------------------------------------------------------

excludes = [
    "tkinter.test",
    "unittest",
    "pydoc",
    "doctest",
    "pytest",
    "IPython",
    "notebook",
    "pandas",       # not used
    "matplotlib",   # not used
    "PyQt5", "PyQt6", "PySide2", "PySide6",  # no Qt in this app
]

# ---------------------------------------------------------------------------
# Analysis / Build
# ---------------------------------------------------------------------------

a = Analysis(
    [ENTRY],
    pathex=[str(SRC_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hidden_imports)),
    hookspath=[str(PROJECT_ROOT / "installer" / "hooks")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

# Icon — only referenced if assets/icon.ico exists (generated by
# scripts/make_icon.py during the build).
icon_path = assets_dir / "icon.ico"
icon_arg = str(icon_path) if icon_path.exists() else None

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # UPX compression breaks CUDA DLLs
    console=False,             # pythonw-style: no terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_arg,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

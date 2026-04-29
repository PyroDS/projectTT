# Changelog

All notable changes to Tachyon Transcripts are documented in this file. The format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [0.1.0] — 2026-04-20 — First public release

Initial shareable release. The app itself has been functional internally for months; this release bundles a proper installer, a first-run UX, and the legal/licence/contributor scaffolding that a public release needs.

### Added
- **Hardware-aware transcriber** (`hardware.py`, `transcriber.py`). Detects NVIDIA GPUs via NVML with a torch fallback, then CPU. Picks an appropriate Whisper model for the hardware: `large-v3` (≥ 10 GB VRAM), `medium` (≥ 6 GB), `small` (< 6 GB), `distil-large-v3` (CPU). Quantization is chosen accordingly (float16 on CUDA, int8 on CPU).
- **CUDA → CPU fallback**. If the requested CUDA model fails to load at startup (missing DLL, outdated driver, no card), the app transparently falls back to a CPU-friendly model and notifies the user via the tray.
- **First-run setup wizard** (`ui/wizard.py`). Five pages: welcome + detected hardware summary, legal disclaimer with required consent checkbox, microphone picker, loopback device picker, done. Blocks the rest of startup until the user either finishes it or closes the window.
- **Consent gate on recording**. The tray's "Start Recording" menu refuses to start a session until the legal disclaimer has been acknowledged.
- **Inno Setup installer** (`installer/`). Wraps a PyInstaller one-folder bundle into an unsigned `.exe` installer. Per-user install, no admin required. Optional post-install step pre-caches the Whisper model.
- **Windows icon** (`scripts/make_icon.py`, `assets/icon.ico`). Multi-resolution ICO generated from the same design as the in-app tray icon.
- **`--download-model` and `--version` CLI flags** (`main.py`). Used by the installer's post-install `[Run]` step and by diagnostics.
- **Public release scaffolding**: `README.md`, `LICENSE` (MIT), `CONTRIBUTING.md`, `CHANGELOG.md`, `docs/LEGAL.md`, and GitHub issue templates.
- **Setup Wizard menu entry** in the system tray (`ui/tray.py`). Lets the user re-run the wizard after first install.

### Changed
- `setup.bat` now calls `scripts\download_model.py` instead of hardcoding `large-v3` + `cuda` + `float16`. Setup now succeeds on machines without NVIDIA GPUs.
- Default `model_size` in `Config` changed from `"large-v3"` to `"auto"`. Existing configs with an explicit model are unaffected.
- Tray's "Set Output Folder…" menu item no longer opens a tkinter dialog on the pystray thread. The dialog is now scheduled on the main tkinter thread via `root.after(0, ...)`.

### Fixed
- **Batch re-transcription crash** when the Whisper model failed to load. `_on_retranscribe` now guards against a missing model and surfaces a tray notification instead of a buried `ValueError`.
- **File descriptor leak in `capture.py`** on stream startup failure. Both the mic and loopback paths now close partially-opened `SoundFile` handles (and the loopback path also closes the PyAudio stream) before propagating the failure.
- **Division-by-zero risk in `batch.py::_normalize_rms`** when given an empty audio array. `_rms` now returns 0.0 for non-finite results (catching the `np.mean` empty-slice NaN), and `_normalize_rms` routes through `_rms` with an explicit `audio.size == 0` early-return.
- **Tray-thread tkinter misuse**: the folder-picker dialog used to construct a second `tk.Tk()` from the pystray worker thread. Refactored so the tray only signals intent and the dialog runs on the existing tkinter main thread.

### Known issues
- The installer is not code-signed. Windows SmartScreen will show a warning on first run.
- If the user declines the post-install model download, the first launch blocks silently during `load_model()` without any progress UI. In-wizard progress bar is a v1.1 item.
- pyannote speaker diarization is optional and not bundled in the installer; users who want it must `pip install pyannote.audio` in the source install.
- No automated test suite yet. All testing is manual, on live recordings. Adding smoke tests is a v1.1 priority.

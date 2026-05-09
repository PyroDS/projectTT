# Changelog

All notable changes to Tachyon Transcripts are documented in this file. The format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

No unreleased changes yet.

## [0.1.2] — 2026-05-08 — First public release

First public release of Tachyon Transcripts, a Windows-only local meeting transcription app. Tachyon captures microphone and system audio locally with WASAPI, transcribes in real time with Whisper, shows live captions, and saves timestamped Markdown transcripts alongside the raw WAV audio. Audio and transcripts are not uploaded to a cloud service.

### Highlights
- Local real-time transcription with Whisper.
- Microphone + system audio capture with separate labels for "You" and "Them".
- Optional multi-device loopback capture for setups with separate chat/game/headset outputs.
- Transparent always-on-top caption overlay with a global hotkey.
- First-run setup wizard for legal consent, microphone selection, and output-device selection.
- Hardware-aware model selection with NVIDIA GPU acceleration when available and CPU fallback when needed.
- Batch re-transcription and speaker diarization tools for improving saved transcripts.
- Installer-based Windows setup with no Python install required.

### Changed
- Tray-first startup now keeps the app visible/responsive while the model loads in the background and surfaces status text in the tray menu.
- Setup-time model pre-download now uses the same hardware resolution policy as runtime, reducing mismatches between downloaded and selected models.

### Fixed
- Real-time chunk timestamps now represent chunk-audio start time (not flush time), improving transcript timing alignment.
- Model-loader worker now guards transcriber construction and handles startup failures through the same tray status/notification path.
- Added deterministic regression coverage for chunk timestamp semantics.

### Notes
- Download `TachyonTranscripts-Setup-0.1.2.exe` from the GitHub release assets and run it.
- The installer is currently unsigned, so Windows SmartScreen may show a warning. Click **More info** → **Run anyway** if you trust the download.
- First launch may take a while if the Whisper model has not been downloaded yet.
- Recording laws vary by location. Read the legal notice before using the app.

## [0.1.0] — 2026-04-20 — Public release preparation

Initial shareable-release preparation. The app itself had been functional internally for months; this work bundled a proper installer, a first-run UX, and the legal/licence/contributor scaffolding that a public release needs.

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
- pyannote speaker diarization is optional and not bundled in the installer; users who want it must `pip install pyannote.audio` in the source install.
- If the user declines post-install model download, first launch can still be slow; tray status is shown, but there is still no dedicated wizard progress bar.

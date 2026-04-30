# Tachyon Transcripts — Implementation Plan

## Context
Build a local-first, covert meeting transcription tool for Windows. Captures microphone + system audio via Windows WASAPI (built-in, no drivers needed), transcribes in real-time using GPU-accelerated Whisper, displays live captions in a minimal overlay, and exports timestamped markdown transcripts with audio files. Core processing is local; initial dependency/model setup may require internet access. Markdown remains the human-readable artifact, while JSON sidecars preserve lossless timing metadata for post-processing accuracy.

**Target hardware**: NVIDIA 2080 Ti (11GB VRAM) — can run Whisper large-v3 comfortably.

## Architecture Overview

```
┌──────────────┐   ┌───────────────────┐
│  Microphone   │   │  System Audio      │
│  (WASAPI)     │   │  (WASAPI Loopback) │
└──────┬────────┘   └────────┬───────────┘
       │ "You"               │ "Them"
       ▼                     ▼
┌──────────────────────────────────────────┐
│          AudioCapture (capture.py)        │
│  - Mic via sounddevice                    │
│  - Loopback via PyAudioWPatch             │
│  - Chunks queued for real-time STT        │
│  - Full audio saved to WAV on disk        │
└────────────────────┬─────────────────────┘
                     │ audio chunks + source label
                     ▼
┌──────────────────────────────────────────┐
│        Transcriber (transcriber.py)       │
│  - faster-whisper (auto hardware select) │
│  - ~3-5 sec chunk processing             │
│  - VAD to skip silence                   │
│  - Returns: text, start_time, end_time   │
└────────────┬─────────────┬───────────────┘
             │             │
             ▼             ▼
┌─────────────────┐ ┌─────────────────────┐
│  Live Overlay    │ │  Session Log        │
│  (overlay.py)    │ │  (session.py)       │
│  - Last 4 lines  │ │  - Accumulates all  │
│  - Semi-transparent│ │  segments in memory│
│  - Hotkey toggle │ │  - Feeds exporter   │
└─────────────────┘ └─────────┬───────────┘
                              │ on stop
                              ▼
                    ┌─────────────────────┐
                    │  Markdown Export     │
                    │  (exporter.py)       │
                    │  - Timestamped MD    │
                    │  - Speaker labels    │
                    │  - Audio file links  │
                    └─────────────────────┘
```

## Project Structure

```
tachyon-transcripts/
├── src/
│   └── tachyon/
│       ├── __init__.py
│       ├── main.py           # Entry point — wires everything together
│       ├── capture.py        # WASAPI audio capture (mic + multi-loopback)
│       ├── transcriber.py    # faster-whisper integration
│       ├── session.py        # Recording session lifecycle + segment storage
│       ├── exporter.py       # Markdown file generation
│       ├── config.py         # User settings (output dir, model size, hotkey)
│       ├── batch.py          # Batch re-transcription engine
│       ├── diarizer.py       # Speaker diarization engine (post-processing)
│       ├── hardware.py       # GPU/CPU detection + model recommendation
│       └── ui/
│           ├── __init__.py
│           ├── tray.py       # System tray icon + menu (pystray)
│           ├── overlay.py    # Transparent caption overlay (tkinter)
│           ├── reviewer.py   # Transcript review + re-transcription window
│           ├── wizard.py     # First-run setup + consent gate
│           ├── theme.py      # Centralized UI theme constants
│           └── widgets.py    # Shared custom tkinter widgets
├── docs/
│   ├── implementation-plan.md
│   ├── architecture.md
│   └── development-log.md
├── requirements.txt
├── requirements-dev.txt
├── run.bat                   # Double-click launcher
├── setup.bat                 # First-time: creates venv, installs deps, downloads models
├── update.bat                # Update dependencies in existing venv
├── debug.bat                 # Console-visible launcher for debugging
└── README.md                 # Share-facing project overview
```

## Implementation Steps

### Step 1: Project Scaffolding
- Create directory structure
- Create `requirements.txt` with dependencies:
  - `sounddevice` — audio capture
  - `faster-whisper` — local Whisper STT (pulls in CTranslate2 + CUDA)
  - `numpy` — audio buffer handling
  - `pystray` — system tray
  - `Pillow` — tray icon image
  - `keyboard` — global hotkey
  - `soundfile` — WAV writing
  - `soxr` — high-quality audio resampling (device native rate → 16kHz for Whisper)
- Create `setup.bat` — creates a local Python venv in the project folder, installs deps, pre-downloads the Whisper model. User runs this once.
- Create `run.bat` — activates venv and launches main.py. This is the daily launcher.

### Step 2: Audio Capture (`capture.py`)
- Use `sounddevice` for mic capture and `PyAudioWPatch` for WASAPI loopback capture
- **Device enumeration**: On startup, enumerate available WASAPI devices and log them. Allow user to select mic/output device via config (default: system default devices). This avoids silent failures from blindly grabbing the wrong device.
- **Mic stream**: Open selected input device at its native sample rate, mono
- **System stream(s)**: Open WASAPI loopback stream(s) on selected output device(s) at native sample rates
- **Resampling**: Capture at each device's native sample rate and resample to 16kHz (what Whisper expects) before queuing. Use `soxr` or `scipy.signal.resample` — don't assume devices will natively support 16kHz.
- Mic runs in sounddevice callback thread; each loopback device runs in a PyAudio callback thread
- Audio chunks (~3 seconds) placed into a shared `queue.Queue` with metadata: `{source: "you"|"them"|"them:Label", audio: np.array, timestamp: float}`
- **Audio file storage**: Save full uncompressed audio to WAV files per session, organized as:
  ```
  output/
  └── 2026-02-22_143000/
      ├── transcript.md
      └── audio/
          ├── mic.wav        # Your voice — full session recording
          └── system.wav     # Their voice — full session recording
  ```
  These files serve as the source of truth for post-processing (re-transcription, diarization) and replay.
- **Graceful fallback**: If loopback capture fails to open (device busy, unsupported), still capture the mic stream and notify the user via tray notification. A mic-only transcript is better than a crash.
- Provide `start()`, `stop()`, `get_devices()` interface

### Step 3: Transcription Engine (`transcriber.py`)
- Load `faster-whisper` with hardware-aware defaults (`model_size="auto"`, `compute_device="auto"`)
- Resolve model/device via runtime hardware detection (`hardware.py`) and support CUDA->CPU fallback on load failure
- Worker thread pulls chunks from the audio queue
- Each chunk transcribed with `model.transcribe()` using VAD filter and `word_timestamps=True` for precise word-level boundaries
- Returns `TranscriptSegment(speaker, text, start_time, end_time)`
- Emits segments via a callback so overlay + session can both consume
- **Chunk boundary handling**: Use a rolling buffer approach instead of naive stitching. Each chunk sent to Whisper is ~3s of new audio prepended with ~1s from the previous chunk. Use word-level timestamps to identify and trim the overlapping portion, keeping only text corresponding to the new audio. This avoids duplicate/split words at boundaries without fragile dedup heuristics.

### Step 4: Session Manager (`session.py`)
- `Session` class — created per recording
- Stores list of `TranscriptSegment` objects
- Tracks session start time (wall clock) and elapsed time
- Provides `add_segment()`, `get_recent(n)`, `get_all()`
- Export is orchestrated by `main.py` on recording stop

### Step 5: Markdown Exporter (`exporter.py`)
- Takes session data + output directory path
- Writes `transcript.md` plus matching `transcript.json` sidecar with exact segment timings
- Generates markdown like:
  ```markdown
  # Meeting Transcript — 2026-02-22 14:30

  **Duration**: 0:45:23
  **Audio**: [Mic Recording](./audio/mic.wav) | [System Recording](./audio/system.wav)

  ---

  **[0:00:05] You:**
  Hey everyone, let's get started with the standup.

  **[0:00:12] Them:**
  Sounds good. I finished the API integration yesterday.

  **[0:00:25] You:**
  Great, any blockers?
  ```
- Creates `audio/` subfolder alongside the markdown file
- Multi-loopback sessions use `system_0.wav`, `system_1.wav`, etc. and `audio/device_manifest.json` for mapping
- Output directory is configurable (default: `./output/` inside project)

### Step 6: System Tray (`ui/tray.py`)
- Uses `pystray` for system tray icon
- Menu items:
  - Start Recording / Stop Recording (toggles)
  - Show/Hide Captions
  - Review Transcripts
  - Set Microphone
  - Loopback Devices
  - Set Output Folder (opens folder picker dialog)
  - Open Output Folder
  - Setup Wizard
  - Quit
- Top-of-menu informational state:
  - Last recorded session timestamp
  - Temporary status text during model loading/failure
- Start Recording remains disabled until the transcription model reports ready
- Tray icon changes color/state when recording (red dot = recording)
- Runs in its own thread, communicates with main via callbacks

### Step 7: Caption Overlay (`ui/overlay.py`)
- `tkinter` transparent, always-on-top window
- Positioned bottom-center of screen (like subtitle placement)
- Shows last 3-4 transcript lines with speaker labels
- Semi-transparent dark background, white text
- Draggable (click and drag to reposition)
- Hotkey toggle: `Ctrl+Shift+T` to show/hide
- Minimal, unobtrusive — looks like closed captions
- **Thread safety**: Tkinter is not thread-safe. Transcript updates from the transcriber thread are placed into a `queue.Queue`. The overlay's tkinter mainloop polls this queue every ~100ms via `root.after()` and updates the display. No direct cross-thread tkinter calls.

### Step 8: Config (`config.py`)
- Simple JSON config file stored in project directory
- Settings: output_dir, model_size, compute_device, hotkey, overlay_position, overlay_opacity, mic_device, output_device, loopback_devices, diarize_backend, hf_token, first_run_complete, consent_acknowledged, reviewer_geometry, overlay_expanded_size
- Defaults that work out of the box — zero config needed to start

### Step 9: Main Entry Point (`main.py`)
- Wires all components together
- Startup sequence:
  1. Load config
  2. Start tkinter root + first-run wizard when needed (consent + device selection)
  3. Start tray + hotkey immediately with "model loading" status
  4. Load transcription model on a background thread
  5. Enable recording once model is ready
  6. On start: create session, start capture, begin transcription
  7. On stop: stop capture/transcriber, finalize transcript, export markdown
- Clean shutdown handling

### Step 10: Launcher Scripts
- `setup.bat`:
  ```bat
  @echo off
  echo [1/6] Locate Python 3.11...
  echo [2/6] Create .venv...
  echo [3/6] Install deps (torch CPU + requirements.txt)...
  echo [4/6] Pre-download Whisper model...
  echo [5/6] Pre-download speechbrain model...
  echo [6/6] Pre-download resemblyzer model...
  echo Setup complete! Run 'run.bat' to start.
  ```
- `run.bat`:
  ```bat
  @echo off
  if not exist ".venv\Scripts\pythonw.exe" (
      echo ERROR: Virtual environment not found. Run setup.bat first.
      exit /b 1
  )
  cd /d "%~dp0src"
  start "" "..\.venv\Scripts\pythonw" -m tachyon.main
  ```
  (`pythonw` = no console window visible — covert)

## Key Technical Details

- **WASAPI Loopback**: Implemented via `PyAudioWPatch` (sounddevice is still used for mic capture). This provides reliable loopback on Windows and supports multi-device loopback capture.
- **Hardware-aware Whisper config**: `large-v3` on >=10 GB VRAM GPUs, `medium` on 6-10 GB, `small` on <6 GB, and `distil-large-v3` on CPU.
- **Graceful fallback**: if CUDA model load fails at startup, retry on CPU with int8 so the app still starts.
- **Rolling buffer with word timestamps**: ~1s overlap prepended to each ~3s chunk. Word-level timestamps from faster-whisper allow precise trimming of the overlap region, eliminating duplicate text at chunk boundaries.
- **VAD**: faster-whisper has built-in Silero VAD — automatically skips silent chunks.
- **Resampling**: Devices are captured at native sample rates and resampled to 16kHz before Whisper. This avoids WASAPI errors from requesting unsupported rates.
- **Threading model**: Audio capture (1 mic callback + N loopback callbacks) → Queue → Transcriber (1 thread) → `queue.Queue` → UI thread (tkinter polls via `root.after()`)
- **Speaker labeling**: "You" (mic) vs "Them" / "Them (Label)" (system audio). Single loopback uses "Them"; multi-loopback uses "Them (Chat)", "Them (Game)", etc. Post-recording diarization splits "Them" into individual speakers.

## "No Install" Strategy
- `setup.bat` creates everything inside the project folder (`.venv/`)
- `run.bat` launches from the local venv — no system-wide Python packages
- Only prerequisite: Python 3.11+ installed on the system (needed for venv creation)
- **Implemented**: PyInstaller + Inno Setup pipeline under `installer/` produces a standalone `.exe` with zero prerequisites. See `docs/architecture.md` (Distribution & Packaging) and `installer/README.md` for the build procedure.

## Verification / Testing
1. Run `setup.bat` — confirm venv created, deps installed, model downloaded
2. Run `run.bat` — confirm tray icon appears, no console window
3. Click "Start Recording" — play some audio (YouTube video), speak into mic
4. Confirm overlay shows live captions with "You" and "Them" labels
5. Click "Stop Recording" — confirm markdown file generated with timestamps
6. Confirm WAV files saved alongside the markdown
7. Test hotkey `Ctrl+Shift+T` toggles overlay visibility
8. Test "Set Output Folder" changes where files are saved

## Audio Storage & Post-Processing

The saved WAV files (mic.wav + system.wav per session) are the foundation for post-session enhancements. Since full uncompressed audio is retained alongside each transcript, we can re-process recordings at any time without needing to re-record.

**Implemented post-processing capabilities:**
- **Re-transcription** (`batch.py`): Re-processes saved WAV files with enhanced Whisper settings (beam search, full-file processing, condition_on_previous_text). Includes crosstalk suppression (RMS energy comparison) and segment deduplication. Produces versioned transcripts (`transcript_v2.md`, etc.).
- **Transcript review** (`ui/reviewer.py`): Browse past sessions, view transcripts with speaker-colored text, switch between original and batch-transcribed versions, trigger re-transcription with progress tracking.
- **Versioned export** (`exporter.py`): `discover_versions()`, `next_version_number()`, `export_transcript_versioned()`, `load_transcript_from_markdown()` — supports multiple transcript versions per session and now writes matching `transcript*.json` sidecars with exact segment timing.

- **Speaker diarization** (`diarizer.py`): Post-processing speaker identification using neural speaker embeddings (speechbrain/pyannote/resemblyzer) + `scikit-learn` AgglomerativeClustering. Splits "Them" into distinct "Speaker 1", "Speaker 2", etc. User can assign real names through inline UI panel. Produces diarized versioned transcripts, JSON sidecars, and `speaker_map.json`. Supports multi-loopback sessions via `device_manifest.json` WAV discovery and now aggregates embeddings from all loopback WAV files instead of selecting only one.
- **Multi-loopback capture** (`capture.py`): Simultaneous capture from multiple WASAPI output devices (e.g., Arctis 7 Chat + Game channels). Each device gets its own WAV file and source tag. `device_manifest.json` written per session for inter-module communication. Tray UI (`tray.py`) provides loopback device selection submenu. All downstream modules (batch, diarizer, exporter, reviewer) discover WAV files via manifest with fallback to `system.wav` for backward compatibility.

**Future enhancements:**
- **Audio replay**: WAV files are directly playable. Timestamps in the markdown map to audio positions, enabling jump-to-timestamp playback in any audio player
- **Combined audio export**: Merge mic.wav + system.wav into a single mixed WAV for sharing or archival
- **Cross-session speaker re-identification**: "John always sounds like this"
- **Real-time diarization hints during recording**

The key design decision: **always save the raw audio.** The audio files are the asset that makes all future enhancements possible.

## Build Order
Steps 1-3 first (scaffolding + audio + transcription) — this is the core engine.
Then 4-5 (session + export) — now we have end-to-end file output.
Then 6-7 (tray + overlay) — UI layer on top.
Then 8-10 (config + main + launchers) — polish and packaging.

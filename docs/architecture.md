# Tachyon Transcripts — Architecture & Design

## System Overview

Tachyon Transcripts is a local-first, real-time meeting transcription tool for Windows. Core recording/transcription/export processing happens on-device using Whisper. Audio/transcript content is not uploaded by the app.

## Data Flow

```
┌──────────────┐   ┌────────────────┐   ┌────────────────┐
│  Microphone   │   │  Loopback #1   │   │  Loopback #N   │
│  (WASAPI)     │   │  (WASAPI LB)   │   │  (WASAPI LB)   │
└──────┬────────┘   └───────┬────────┘   └───────┬────────┘
       │ "you"              │ "them" or           │ "them:Label"
       │ (native rate)      │ "them:Label"        │ (native rate)
       ▼                    ▼                     ▼
┌──────────────────────────────────────────────────────────┐
│              AudioCapture (capture.py)                    │
│  - Mic via sounddevice, loopback(s) via PyAudioWPatch   │
│  - Multi-loopback: separate stream/WAV per device        │
│  - Resample to 16kHz via soxr                            │
│  - Chunks queued for real-time STT                       │
│  - Full audio saved: mic.wav + system*.wav on disk       │
│  - device_manifest.json maps files to devices/labels     │
│  - Graceful fallback if loopback fails                   │
└──────────────────────────┬───────────────────────────────┘
                           │ audio chunks + source label
                           │ (queue.Queue)
                           ▼
┌──────────────────────────────────────────┐
│        Transcriber (transcriber.py)       │
│  - faster-whisper (hardware-aware auto)  │
│  - Rolling buffer: profile-driven overlap │
│  - word_timestamps=True for trim logic   │
│  - VAD to skip silence                   │
│  - Returns: text, start_time, end_time   │
└────────────┬─────────────┬───────────────┘
             │             │
             │ callback     │ callback
             ▼             ▼
┌─────────────────┐ ┌─────────────────────┐
│  Live Overlay    │ │  Session Log        │
│  (overlay.py)    │ │  (session.py)       │
│  - queue.Queue   │ │  - Accumulates all  │
│  - root.after()  │ │    segments in mem   │
│  - Last 4 lines  │ │  - Feeds exporter   │
│  - Semi-transparent│                      │
│  - Hotkey toggle │ │                     │
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

## Module Responsibilities

### `capture.py` — Audio Capture
- **Owns**: All audio I/O — device enumeration, stream management, WAV writing
- **Inputs**: Device config (which mic, which output devices)
- **Outputs**: Audio chunks to a shared `queue.Queue`, WAV files on disk, `device_manifest.json`
- **Key details**:
  - Enumerates WASAPI devices on init, selects based on config or defaults
  - Captures at device native sample rate, resamples to 16kHz mono via `soxr`
  - Mic stream via sounddevice; loopback stream(s) via PyAudioWPatch (sounddevice's PortAudio DLL lacks WASAPI loopback support)
  - **Multi-loopback**: supports capturing from multiple output devices simultaneously (e.g., Arctis 7 Chat + Game). Each loopback device gets its own `_LoopbackState` with independent stream, WAV writer, buffer, and resampler.
  - Chunk duration is driven by `live_caption_mode`: `fast`=1.5s, `balanced`=2.0s (default), `accurate`=3.0s
  - Queue items: `AudioChunk(source="you"|"them"|"them:Label", audio=np.ndarray, timestamp=float)`
  - Source tag convention: `"you"` (mic), `"them"` (single loopback, backward compatible), `"them:Chat"` / `"them:Game"` (multi-loopback with labels)
  - Writes full session audio: `mic.wav` + `system.wav` (single) or `system_0.wav`, `system_1.wav`, ... (multi)
  - Writes `device_manifest.json` in `audio/` directory mapping filenames to device names, labels, and source tags
  - If loopback fails: logs warning, notifies user via tray callback, continues with mic only

### `transcriber.py` — Transcription Engine
- **Owns**: Whisper model lifecycle, audio-to-text conversion
- **Inputs**: Audio chunks from the queue
- **Outputs**: `TranscriptSegment` objects via callback
- **Key details**:
  - Loads `faster-whisper` model on startup — takes a few seconds
  - **Hardware-aware model selection** (see `hardware.py`): if config sets `model_size="auto"` / `compute_device="auto"`, the transcriber auto-picks based on detected GPU: `large-v3`/CUDA for ≥10 GB VRAM, `medium`/CUDA for 6–10 GB, `small`/CUDA for <6 GB, `distil-large-v3`/CPU for no GPU
  - **Graceful CUDA→CPU fallback**: if CUDA load fails (missing DLL, OOM, etc.), automatically retries on CPU with `int8` compute type. `fell_back_to_cpu` property exposed for `main.py` to notify user via tray.
  - **Fatal runtime-error classification**: if GPU inference fails mid-recording (missing `cublas64_12.dll`, missing `cudnn64_9.dll`, driver mismatch), transcriber reports a one-shot fatal error callback so `main.py` can warn the user and avoid silent empty transcripts.
  - Worker thread consumes from the audio queue
  - Rolling buffer and decode settings are mode-driven:
    - `fast`: 0.5s overlap + 1.5s new audio, beam_size=1
    - `balanced`: 0.75s overlap + 2.0s new audio, beam_size=1
    - `accurate`: 1.0s overlap + 3.0s new audio, beam_size=2
  - Transcribes with `word_timestamps=True` to get precise word boundaries
  - Trims output to only include words from the new audio portion (based on timestamp comparison)
  - VAD (Silero, built into faster-whisper) skips silent chunks automatically
  - Emits `TranscriptSegment(speaker: str, text: str, start_time: float, end_time: float, words?: list[WordTiming])` via callback; word timings are session-relative and stored in JSON sidecars for diarization

### `session.py` — Session Manager
- **Owns**: Recording session lifecycle, segment accumulation
- **Inputs**: `TranscriptSegment` objects from transcriber callback
- **Outputs**: Ordered list of all segments, session metadata
- **Key details**:
  - One `Session` instance per recording
  - Tracks wall-clock start time and elapsed duration
  - `add_segment()` — thread-safe append
  - `get_recent(n)` — last N segments for overlay display
  - `get_all()` — full transcript for export
  - Export is triggered by `main.py` on recording stop (session is storage/lifecycle only)

### `exporter.py` — Transcript Export
- **Owns**: File generation — markdown transcript + JSON sidecar + audio file organization
- **Inputs**: Session data (segments + metadata), output directory path
- **Outputs**: Markdown file, JSON sidecar (`transcript*.json`), + audio subfolder on disk
- **Key details**:
  - Creates per-session output folder: `output/YYYY-MM-DD_HHMMSS/`
  - Generates `transcript.md` with header (date, duration, audio links) + timestamped speaker-labeled lines
  - Writes a lossless sidecar (`transcript.json` / `transcript_vN.json`) with exact float `start`/`end` timings per segment and optional per-word timings (schema v2)
  - Audio files are moved/copied into `audio/` subfolder alongside the markdown

### `hardware.py` — Hardware Detection
- **Owns**: GPU/CPU/VRAM detection and model-size recommendation policy
- **Inputs**: None — detects host hardware at runtime
- **Outputs**: `HardwareInfo(has_cuda, cuda_device_name, vram_gb, reason)` + recommended model size / compute type
- **Key details**:
  - Detection order: NVML (fastest, no torch needed) → `torch.cuda` → CPU-only fallback
  - Never raises — all detection paths are guarded
  - `resolve_transcriber_config(device, model)` expands `"auto"` values to concrete `(device, model_size, compute_type, hw)` tuples
  - Consumed by `transcriber.load_model()` at startup and by `ui/wizard.py` to show users the detected hardware

### `config.py` — Configuration
- **Owns**: User preferences, defaults
- **Inputs**: JSON config file on disk
- **Outputs**: Config object used by all modules
- **Settings**: output_dir, model_size, compute_device, live_caption_mode, hotkey, overlay_position, overlay_opacity, mic_device, output_device, loopback_devices, diarize_backend, hf_token, first_run_complete, consent_acknowledged, reviewer_geometry, reviewer_tutorial_show_on_open, overlay_expanded_size
- **Defaults**: Everything works out of the box with zero config — `model_size="auto"` and `compute_device="auto"` let the transcriber pick based on detected hardware.

### `ui/tray.py` — System Tray
- **Owns**: System tray icon, menu, user interaction
- **Inputs**: Application state callbacks
- **Outputs**: User action callbacks (start/stop, show/hide, quit)
- **Key details**:
  - Uses `pystray` for native Windows system tray
  - Menu: Start/Stop Recording, Show/Hide Captions, Review Transcripts, Set Microphone, Loopback Devices, Live Caption Mode, Set Output Folder, Setup Wizard, Quit
  - Shows informational status text while model loads/fails and a last-session timestamp row
  - Start Recording is disabled until model load completes (`set_model_ready(True)`)
  - Icon changes state when recording (visual indicator)
  - Runs in its own thread
  - **Callback signatures are intent forwards** — mode/device menu selections pass small values (e.g. selected mode/device), while tkinter-affecting handlers (folder picker, wizard) are invoked as intents and scheduled onto the tkinter main thread via `root.after(0, ...)`. Tkinter widgets are never created from the pystray thread.

### `ui/wizard.py` — First-Run Setup Wizard
- **Owns**: Initial user onboarding + legal consent gate
- **Inputs**: `Config`, `HardwareInfo`, enumerated mic + loopback device lists
- **Outputs**: Modifies config in-place (mic_device, loopback_devices, first_run_complete, consent_acknowledged)
- **Key details**:
  - 5-page modal `tk.Toplevel`: (1) welcome + detected hardware summary, (2) recording-law disclaimer with required checkbox, (3) mic picker, (4) loopback picker, (5) done
  - Consent checkbox is the legal-liability gate: `_on_start_recording()` refuses to start until `consent_acknowledged` is True, even on subsequent launches
  - `first_run_complete` is set only on Finish — closing the wizard early re-opens it next launch
  - Can be re-opened from the tray "Setup Wizard" menu item; same code path, same persistence
  - Runs inside the tkinter mainloop via `root.after(0, ...)` so the Toplevel is modal (tkinter modals require a running mainloop)

### `ui/theme.py` — Centralised Visual Theme
- **Owns**: All colours, fonts, dimensions, and the ToolTip helper class
- **Key details**:
  - `Color` class: semantic colour tokens (bg_primary, fg_primary, accent, danger, etc.)
  - `Font` class: family ("Segoe UI") and standard size constants (body=12, title=14, etc.)
  - `Dim` class: layout dimensions (reviewer window min/default size, sidebar width, toolbar height, overlay dimensions)
  - `OVERLAY_SPEAKER_PALETTE`: 5-colour palette for non-"You" speakers in the overlay
  - `ToolTip` class: lightweight hover tooltip with 500ms delay, auto-positions below widget

### `ui/widgets.py` — Shared Custom Tk Widgets
- **Owns**: Reusable themed controls used by reviewer/overlay
- **Key details**:
  - `HoverButton`, `GlowFrame`, `GradientBar`, `PulseIndicator`, and `SessionCard`
  - Centralises non-trivial widget behavior (hover animation, pulse, card selection state)

### `ui/overlay.py` — Caption Overlay
- **Owns**: Live caption display
- **Inputs**: Transcript segments via `queue.Queue`
- **Outputs**: On-screen transparent overlay window
- **Key details**:
  - `tkinter` always-on-top transparent window with 1px border
  - Bottom-center screen position (subtitle-style), with final placement recalculated after collapsed height is known to keep consistent bottom anchoring
  - Shows last 4 lines with speaker labels
  - Displays muted placeholder text when caption history is empty (startup/new session)
  - Thread safety: polls `queue.Queue` every ~100ms via `root.after()`
  - Draggable, hotkey toggle (`Ctrl+Shift+T`)
  - Semi-transparent dark background, white text
  - Pulsing red recording indicator dot in titlebar
  - Segment dividers between different speakers in expanded mode
  - Smooth fade transitions on show/hide

### `batch.py` — Batch Re-transcription Engine
- **Owns**: Post-recording re-processing of saved WAV files with enhanced quality
- **Inputs**: Session directory with `audio/mic.wav` and loopback WAV(s) (discovered via `device_manifest.json` or fallback to `system.wav`)
- **Outputs**: Re-transcribed `TranscriptSegment` list, versioned markdown export
- **Key details**:
  - Processes full WAV files (no 3s chunking) with beam search (`beam_size=5`)
  - VAD + `condition_on_previous_text=True` for coherent segmentation
  - Crosstalk suppression: compares RMS energy between channels to detect bleed-through
  - Deduplication: removes near-duplicate segments from different channels
  - Shares the same `WhisperModel` instance as real-time transcriber (no VRAM duplication)
  - Reports progress via callback for UI updates
  - Cancellable via `threading.Event`

### `diarizer.py` — Speaker Diarization Engine
- **Owns**: Post-processing speaker identification for session audio
- **Inputs**: Session directory with loopback WAV(s) and/or `mic.wav` (via `device_manifest.json` or fallbacks) and a source transcript (JSON sidecar preferred, markdown fallback)
- **Outputs**: Relabeled `TranscriptSegment` list with "Speaker N" labels, `SpeakerInfo` profiles, diarized markdown export, `speaker_map.json`
- **Key details**:
  - Three audio source modes (reviewer toolbar **Source** dropdown):
    - **system**: uses loopback WAV(s), preserves "You" segments, relabels non-"You" only
    - **mixed**: uses `mic.wav` (or configured override), relabels all transcript segments — for single-file recordings with multiple speakers
    - **auto**: uses system when ≥2 non-"You" segments exist, otherwise falls back to mixed when `mic.wav` is available
  - Four switchable diarization backends (user selects from reviewer UI dropdown):
    - **speechbrain** (default): ECAPA-TDNN embeddings + local clustering pipeline
    - **pyannote** (optional): embedding model + local clustering pipeline; requires `pyannote.audio==3.4.0`
    - **pyannote_community** (optional, high accuracy): full local `pyannote/speaker-diarization-community-1` pipeline with exclusive speaker turns and word-level alignment; requires `pyannote.audio` 4.x + HF token/terms
    - **resemblyzer** (lightweight fallback): 256-dim GE2E embeddings + local clustering pipeline
  - All processing is local — embeddings and clustering run on CPU; Hugging Face is only used to download/cache model weights
  - Embedding backends use sliding window extraction + clustering + word/timeline alignment
  - Community-1 backend uses modular code under `tachyon/diarization/` and returns speaker turns directly from pyannote, then aligns transcript words to those turns
  - Multi-loopback aggregation: embeddings from all loopback WAVs are clustered together in system mode
  - 250ms speaker timeline built from window labels via majority vote
  - When JSON sidecar word timings are present, each word is aligned to the timeline and segments are split at speaker-change boundaries; otherwise whole-segment majority voting is used
  - Transcript segments relabeled from timeline using exact segment `start`/`end` timing from sidecar data
  - Optional fixed speaker-count hint (`num_speakers`) can be supplied from reviewer UI
  - Speaker map persisted as `speaker_map.json` for user-assigned names
  - Backend choice + HF token persisted in `config.json`, remembered across restarts
  - No Whisper model needed -- can technically run alongside recording, but kept mutually exclusive for simplicity
  - Reports progress via callback, cancellable via `threading.Event`

### `ui/reviewer.py` — Transcript Review Window
- **Owns**: Browsing past sessions, re-transcription, and speaker diarization UI
- **Inputs**: Output directory, session folders
- **Outputs**: User actions (re-transcribe, diarize, version selection, speaker naming)
- **Key details**:
  - tkinter `Toplevel` window parented to the overlay's `Tk` root
  - Left panel: session list scanned from `output/` directory
  - Right panel: transcript viewer with multi-speaker coloring (8-color palette by order of appearance)
  - Inline speaker panel: shown between header and transcript after diarization — per-speaker rows with color dot, name entry, duration, sample text. Also accessible via "Edit Speakers" link on diarized versions.
  - Version dropdown: switch between original, batch, and diarized versions
  - Top toolbar: re-transcribe/diarize/edit controls, progress, backend + speaker-count + pyannote setup controls (`Accept Terms`, `HF Token`), open-folder
  - When backend is `pyannote`, reviewer exposes one-click browser links to the pinned Hugging Face model page and token settings for setup only; audio/transcript processing remains local
  - Pyannote model-access failures surface a recovery dialog (`Open Model Page`, `Edit Token`, `Use SpeechBrain`) instead of a generic audio-missing error
  - Context tutorial overlay: dynamic multi-step walkthrough shown when Review opens (default), implemented as a single floating tutorial card plus an in-reviewer high-contrast target border highlight (no extra transparent overlay windows), with step-aware card placement and live sync while the reviewer window moves/resizes. Persisted "show on open" preference and toolbar Help button reopen it on demand
  - Bottom status bar: session count + shortcut hints
  - Session discovery via regex matching `YYYY-MM-DD_HHMMSS` folder names

### `main.py` — Entry Point
- **Owns**: Component wiring, startup/shutdown sequence
- **Startup**: Load config → run first-run wizard when needed → start tray/hotkey immediately → load model on background thread → enable recording when ready
- **Recording flow**: Create session → Start capture → Begin transcription → User stops → Export
- **Batch flow**: Open reviewer → Select session → Re-transcribe → Export versioned markdown
- **Diarize flow**: Open reviewer → Select session + version → Identify Speakers → Inline speaker panel → Save Names → Export diarized markdown
- **Mutual exclusion**: Recording, batch, and diarization cannot run concurrently
- **Shutdown**: Clean teardown of all threads and resources

## Threading Model

```
Thread 1 (Main/UI):     tkinter mainloop — overlay + reviewer display, root.after() polling
Thread 2 (Tray):        pystray event loop — system tray menu
Thread 3 (Mic Capture): sounddevice callback — mic audio → queue + WAV (recording only)
Thread 4+ (Loopback):   PyAudioWPatch callbacks — per-device loopback audio → queue + WAV (recording only)
Thread 5 (Transcriber): Real-time worker — pulls from queue, runs Whisper (recording only)
Thread 6 (Batch/Diarize): BatchTranscriber or Diarizer worker — re-transcription or speaker ID (review only)
```

Threads 5 and 6 share the same `WhisperModel` and must not run concurrently.
Thread 6 is shared between batch re-transcription and diarization (also mutually exclusive).
Mutual exclusion enforced via UI state (buttons disabled), not locks.

All inter-thread communication uses `queue.Queue` (thread-safe) and `root.after()` for tkinter updates.

## Output File Layout

Each recording session produces:
```
output/
└── YYYY-MM-DD_HHMMSS/
    ├── transcript.md           # Original real-time transcript
    ├── transcript.json         # Lossless sidecar for transcript.md
    ├── transcript_v2.md        # Batch re-transcribed (if re-transcribed)
    ├── transcript_v2.json      # Lossless sidecar for transcript_v2.md
    ├── transcript_v3.md        # Diarized (if speaker identification run)
    ├── transcript_v3.json      # Lossless sidecar for transcript_v3.md
    ├── speaker_map.json        # Speaker name mapping (if diarized)
    └── audio/
        ├── device_manifest.json  # Maps WAV files to devices/labels
        ├── mic.wav               # Full mic recording (your voice)
        ├── system.wav            # Single loopback (backward compatible)
        ├── system_0.wav          # Multi-loopback: first device (e.g. Chat)
        └── system_1.wav          # Multi-loopback: second device (e.g. Game)
```

**Note:** Single-loopback sessions produce `system.wav`. Multi-loopback sessions produce
`system_0.wav`, `system_1.wav`, etc. The `device_manifest.json` maps each file to its
source device and label. Old sessions without a manifest fall back to `mic.wav`/`system.wav`.

## Distribution & Packaging

Two install paths are supported:

**Developer install (source)** — `setup.bat` provisions a `.venv\` with all runtime
dependencies and pre-downloads the models via `scripts\download_model.py`. Launch via
`run.bat`. Requires Python 3.11 on the host.

**End-user install (binary)** — An Inno Setup installer wraps a PyInstaller one-folder
bundle. No Python install on the host required; the embeddable interpreter + all native
DLLs ship inside the installer. Built from the `installer/` directory:

```
installer/
├── tachyon.spec            # PyInstaller spec — pulls in faster-whisper, ctranslate2,
│                           #   sounddevice, PyAudioWPatch, speechbrain, CUDA DLLs
├── Tachyon.iss             # Inno Setup 6 script — wraps dist\TachyonTranscripts\ in
│                           #   a single setup.exe, creates Start Menu shortcuts
├── build_installer.bat     # End-to-end build (PyInstaller → Inno Setup)
├── hooks/hook-webrtcvad.py # Local PyInstaller hook override for webrtcvad metadata
├── pre_install_notice.txt  # Legal disclaimer shown in the install wizard
└── README.md               # Build procedure + known rough edges
```

Installer CLI flags honoured by `main.py`:

- `--version` — print version and exit (used by diagnostics, kept in sync with `Tachyon.iss`'s `MyAppVersion`).
- `--download-model` — pre-cache the Whisper model for the detected hardware, then
  exit. The Inno Setup `[Run]` section offers this as an optional post-install step so
  first launch isn't blocked on a multi-minute download.

Install-time behaviour:

- Per-user install at `%LocalAppData%\Programs\Tachyon Transcripts` — no admin
  elevation (the installer is unsigned and forcing UAC adds no value).
- Bundled CUDA runtime DLLs are placed in `{app}\_internal\cuda\` (`cublas64_12.dll`,
  `cudnn64_9.dll`, plus supporting CUDA runtime libraries). Installer build now
  fails early if required CUDA DLLs are missing from the PyInstaller inputs.
- Windows `assets/icon.ico` generated by `scripts\make_icon.py` from the same design as the
  tray icon, packed as a multi-resolution ICO (16/32/48/64/128/256 px).
- Uninstall first terminates the running tray process, then removes app/runtime
  artifacts (`_internal`, binaries, docs/assets, `{app}\models`, `config.json`,
  `tachyon.log`, and app shortcuts). Recordings under `{app}\output\` are
  deliberately preserved.

Known rough edges (tracked for v1.1):

- Installer is not code-signed — SmartScreen will show a first-run warning.
- If the user declined post-install model pre-download, first launch can still take a while for model fetch/load.
  Tray status text is shown during this phase; wizard still has no dedicated progress UI.

## Key Constraints
- **Local processing after setup** — runtime transcription/export is local; first-time setup/model downloads require internet unless caches are pre-seeded
- **Windows only** — WASAPI is Windows-specific
- **Hardware-flexible** — NVIDIA GPU recommended (CUDA + float16 for best speed), but runs on CPU with `distil-large-v3` + int8 quantization as a fallback. Auto-detected at startup; CPU fallback also kicks in if CUDA load fails (missing DLL, driver mismatch, etc.).
- **Python 3.11+** — required for venv creation and modern features

# Tachyon Transcripts — AI Agent Rules

## Project Overview
Tachyon Transcripts is a local-first, covert meeting transcription tool for Windows. It captures mic + system audio via WASAPI, transcribes in real-time using GPU-accelerated Whisper, shows live captions, and exports timestamped Markdown transcripts with audio files. Runtime processing is local; initial dependency/model setup may require internet access.

## Mandatory: Read Docs Before Working
**Every agent MUST read the relevant documentation before making any changes.** This is non-negotiable.

Before starting any task:
1. Read `docs/implementation-plan.md` — the full technical spec and architecture
2. Read `docs/architecture.md` — system design, data flow, and module responsibilities
3. Read `docs/development-log.md` — current progress, what's done, what's next, and any open issues

If your task touches a specific module, understand how it fits into the overall architecture before writing code. Do not deviate from the documented design without updating the docs first.

## Documentation Must Stay Current
After completing any task:
- Update `docs/development-log.md` with what was done, any decisions made, and any issues encountered
- If the implementation diverges from the plan, update `docs/implementation-plan.md` and `docs/architecture.md` to reflect the actual state
- Never leave docs out of sync with code

## Tech Stack
- **Language**: Python 3.11+
- **Audio**: `sounddevice` (WASAPI mic), `PyAudioWPatch` (WASAPI loopback), `soxr` (resampling), `soundfile` (WAV I/O)
- **Transcription**: `faster-whisper` (CTranslate2 + CUDA), Whisper large-v3
- **UI**: `tkinter` (overlay), `pystray` (system tray), `Pillow` (icons)
- **Hotkeys**: `keyboard`
- **Target GPU**: NVIDIA 2080 Ti (11GB VRAM)

## Project Structure
```
TachyonTranscripts/
├── CLAUDE.md                    # This file — agent rules
├── README.md                    # Share-facing overview and setup
├── docs/
│   ├── implementation-plan.md   # Full technical spec
│   ├── architecture.md          # System design & data flow
│   └── development-log.md      # Work log & progress tracker
├── src/
│   └── tachyon/
│       ├── __init__.py
│       ├── main.py              # Entry point — wires everything together
│       ├── capture.py           # WASAPI audio capture (mic + multi-loopback)
│       ├── transcriber.py       # faster-whisper integration
│       ├── session.py           # Recording session lifecycle
│       ├── exporter.py          # Markdown file generation
│       ├── config.py            # User settings (JSON config)
│       ├── batch.py             # Batch re-transcription engine
│       ├── diarizer.py          # Speaker diarization engine
│       └── ui/
│           ├── __init__.py
│           ├── tray.py          # System tray icon + menu
│           ├── overlay.py       # Transparent caption overlay
│           └── reviewer.py      # Transcript review + re-transcription window
├── tests/                       # Deterministic pytest suite
├── output/                      # Default transcript output directory
├── requirements.txt
├── requirements-dev.txt         # Test/developer dependencies
├── run.bat                      # Daily launcher
├── setup.bat                    # First-time setup
├── update.bat                   # Dependency refresh for existing venv
└── debug.bat                    # Console-visible launcher for debugging
```

## Code Conventions
- Keep modules focused — each file has one job
- Use type hints on all function signatures
- Use dataclasses for data structures (e.g., TranscriptSegment)
- Thread safety: never call tkinter from background threads — use queue.Queue + root.after()
- Handle errors gracefully — log and notify user via tray, don't crash silently
- Capture audio at device native sample rate, resample to 16kHz for Whisper
- All file paths should use `pathlib.Path`, not string concatenation

## Key Design Decisions
- Binary speaker labels only in v1: "You" (mic) vs "Them" (system audio)
- Rolling buffer for chunk boundaries: prepend ~1s of previous audio, trim via word timestamps
- Always save raw WAV audio — it's the foundation for future post-processing
- Graceful degradation: if loopback fails, continue with mic only

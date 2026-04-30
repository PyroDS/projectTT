# Tachyon Transcripts

A 100% local, Windows-only meeting transcription tool. Captures your microphone and any system (meeting) audio via WASAPI, transcribes in real time with Whisper (GPU-accelerated when an NVIDIA card is available, CPU otherwise), shows live captions, and saves timestamped Markdown transcripts alongside the raw audio files.

Nothing leaves your machine.

> **Legal notice — please read before using.** Tachyon Transcripts is a recording tool. Recording other people without their knowledge or consent is illegal in many jurisdictions. **We are not lawyers and nothing in this project is legal advice. You are solely responsible for checking the laws that apply to you and using this software lawfully.** See [docs/LEGAL.md](docs/LEGAL.md) and the first-run wizard's consent page for a non-lawyer overview of the questions to ask. The app will not let you start a recording until you have acknowledged the disclaimer.

## Why this exists

Every meeting-transcription tool I could find either (a) uploaded audio to somebody else's server, (b) required a subscription, or (c) was bolted into one specific meeting platform. I wanted something that worked for any audio my computer played — Zoom, Teams, Meet, a phone call through a headset, a YouTube video I was taking notes on — without sharing the contents with a third party. Tachyon Transcripts is what I built for myself and now share with others who want the same.

## Features

- **Dual-channel capture** — microphone + system audio as separate streams, so "you" vs "them" labelling is reliable and post-processing (crosstalk suppression, speaker diarization) has clean inputs to work from.
- **Multi-device loopback** — capture multiple output devices simultaneously (e.g. "Chat" headset + "Game" speakers) and keep them as separate channels in the output.
- **Live captions** — transparent, always-on-top overlay with the last few lines of transcription, draggable, toggleable via a global hotkey.
- **Hardware-aware model selection** — large-v3 on a 10 GB+ NVIDIA card, medium on smaller GPUs, distil-large-v3 (int8) on CPU. Auto-detected; override in the config if needed.
- **Graceful CPU fallback** — if CUDA fails at startup (missing DLL, outdated driver, no NVIDIA card), the app falls back to a CPU-friendly model and tells you in a tray notification.
- **Offline batch re-transcription** — once a recording is saved, re-run Whisper with beam search + VAD + crosstalk suppression + deduplication for noticeably better accuracy than the real-time pass.
- **Speaker diarization** — identify who spoke when, using your choice of SpeechBrain ECAPA-TDNN (default), pyannote (optional, higher accuracy, requires a HuggingFace token), or Resemblyzer (lightweight fallback).
- **First-run setup wizard** — hardware detection, legal consent, mic + loopback device picker.
- **Consent gate** — recording is disabled until the disclaimer is acknowledged.
- **No telemetry** — no analytics, no network calls, no cloud services. The only outbound request is the first-time model download from HuggingFace (which you can pre-cache from the installer).

## Install

Two paths. Pick whichever suits you.

### A. Installer (recommended for end users)

Download `TachyonTranscripts-Setup-<version>.exe` from the [Releases page](https://github.com/PyroDS/projectTT/releases) and run it. No Python install required.

**Heads-up**: the installer is not code-signed, so Windows SmartScreen will show a "Windows protected your PC" warning on first run. Click **More info** → **Run anyway**. This is a known rough edge that costs real money to fix (EV code-signing certificate + a few months of reputation building) and is on the v1.1 roadmap.

Install location is per-user: `%LocalAppData%\Programs\Tachyon Transcripts`. No admin elevation required. Uninstall via Settings → Apps → Tachyon Transcripts removes app/runtime files and shortcuts; recordings under `output/` are preserved.

### B. From source (for developers)

Requires Python 3.11 on Windows 10/11. NVIDIA GPU with CUDA 12 drivers recommended but not required.

```
git clone https://github.com/PyroDS/projectTT.git
cd projectTT
setup.bat
run.bat
```

`setup.bat` creates a `.venv\`, installs all dependencies (including `torch` and the CUDA runtime wheels), pre-downloads the Whisper model for your detected hardware, and sets up the speaker-embedding models. Expect ~5–10 minutes depending on network speed.

`run.bat` launches the app without a console window. The app appears in your system tray; right-click for the menu.

To rebuild the installer from source (requires [Inno Setup 6](https://jrsoftware.org/isdl.php)):

```
installer\build_installer.bat
```

See [`installer/README.md`](installer/README.md) for the full build notes.

## Quick start

1. Launch the app. The first-run wizard appears.
2. Read the legal disclaimer carefully and tick the consent checkbox.
3. Pick your microphone from the dropdown.
4. Tick the output devices you want to record from (usually just one — your default speakers or headset).
5. Click Finish.
6. Right-click the tray icon → **Start Recording**.
7. When you're done, right-click the tray icon → **Stop Recording**.
8. Your transcript and audio files are saved to `output/<timestamp>/`.

To review or re-transcribe an older session: right-click the tray icon → **Review Transcripts**.

Default hotkey: `Ctrl+Shift+T` toggles the caption overlay. Customisable in `config.json`.

## Output layout

Each recording produces a timestamped folder:

```
<output-dir>/2026-04-20_143012/
├── transcript.md          # Real-time transcript
├── transcript_v2.md       # (optional) batch-reprocessed transcript
├── transcript_v3.md       # (optional) diarized transcript
├── speaker_map.json       # (optional) speaker name assignments
└── audio/
    ├── device_manifest.json
    ├── mic.wav            # Your voice
    └── system.wav         # Everyone else
```

Multi-device recordings produce `system_0.wav`, `system_1.wav`, etc. and use the manifest to keep track of which WAV came from which device.

## Configuration

All settings live in `config.json` (next to the app). Relevant keys:

| Key | Default | Purpose |
|---|---|---|
| `output_dir`        | `""` (→ `output/` next to the app) | Where recordings go. |
| `model_size`        | `"auto"` | `auto`, `large-v3`, `medium`, `small`, `distil-large-v3`. |
| `compute_device`    | `"auto"` | `auto`, `cuda`, `cpu`. |
| `hotkey`            | `"ctrl+shift+t"` | Global hotkey for the caption overlay toggle. |
| `overlay_opacity`   | `0.8`  | Caption overlay transparency (0.0–1.0). |
| `diarize_backend`   | `"speechbrain"` | `speechbrain`, `pyannote`, `resemblyzer`. |
| `hf_token`          | `""`   | HuggingFace token, required only for the pyannote backend. |

## Troubleshooting

**"Loading model…" takes forever on first launch.** The Whisper model is being downloaded (size depends on hardware/model; often ~600 MB to ~1 GB). During this phase the tray menu shows a status line and recording stays disabled until loading finishes. If you're on the installer and declined the post-install pre-download, the model is fetched on first launch instead. Later launches are fast.

**"Failed to load the transcription model."** Check `tachyon.log` in the install directory. Common causes: no internet on first run, antivirus quarantined a CUDA DLL, graphics driver older than CUDA 12 requires. The app will still load but recording is disabled — fix the cause and restart.

**"System audio capture unavailable — recording mic only."** WASAPI loopback couldn't open the selected output device. This usually means the device is in exclusive mode (some pro audio drivers default to this). Try a different output device or set it to shared mode in Windows sound settings.

**SmartScreen warns on installer launch.** Expected — the installer isn't code-signed. Click "More info" → "Run anyway".

**Antivirus flags the app.** PyInstaller executables sometimes trigger heuristic flags because the bootloader pattern is also used by malware. If your AV lets you, submit the file for analysis or add an exception. This improves as the app builds download reputation.

## Legal

**We are not lawyers, and nothing in this README, in [docs/LEGAL.md](docs/LEGAL.md), in the installer, or in the first-run wizard is legal advice.** Tachyon Transcripts is open-source software released publicly to anyone who wants it. What you use it for, where you use it, and who you record is entirely your responsibility.

Recording laws vary widely by country, state, and even municipality, and they change over time. Some places require all-party consent; some have one-party consent; some have rules that depend on whether the conversation is "private," whether you are a party to it, what platform it is on, your relationship to the other participants, or whether your employer or industry has its own policy. We cannot tell you what applies to your situation — only a lawyer in your jurisdiction can.

**Before recording anything, check the laws, contracts, employer policies, and platform terms of service that apply to you.** If you are unsure, do not record. By downloading, building, installing, or using this software you accept full responsibility for compliance with all applicable rules and agree that the authors and contributors are not liable for how you use it.

A longer (still non-lawyer) overview of the kinds of questions to ask is in [docs/LEGAL.md](docs/LEGAL.md), and an abbreviated version is shown in the installer and the first-run wizard. Treat any of it as a starting point for your own research, not as a substitute for legal advice.

The software is provided "as is" with no warranty of any kind — see [LICENSE](LICENSE).

## Contributing

Bug reports, feature requests, and pull requests welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, coding conventions, and documentation requirements.

## License

[MIT](LICENSE). No warranty; use at your own risk.

## Acknowledgments

Built on the shoulders of:

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — the CTranslate2-based Whisper runtime that makes real-time transcription possible on consumer GPUs.
- [CTranslate2](https://github.com/OpenNMT/CTranslate2) — the fast, quantization-aware inference engine underneath faster-whisper.
- [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) — the only way to do WASAPI loopback from Python without writing a COM wrapper.
- [SpeechBrain](https://github.com/speechbrain/speechbrain), [pyannote.audio](https://github.com/pyannote/pyannote-audio), [Resemblyzer](https://github.com/resemble-ai/Resemblyzer) — speaker embedding and diarization backends.
- [pystray](https://github.com/moses-palmer/pystray) — Windows system tray.

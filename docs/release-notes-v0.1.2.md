# Tachyon Transcripts v0.1.2

First public release of Tachyon Transcripts, a Windows-only local meeting transcription app.

Tachyon captures microphone and system audio locally with WASAPI, transcribes in real time with Whisper, shows live captions, and saves timestamped Markdown transcripts alongside the raw WAV audio. Audio and transcripts are not uploaded to a cloud service.

## Highlights

- Local real-time transcription with Whisper.
- Microphone + system audio capture with separate labels for "You" and "Them".
- Optional multi-device loopback capture for setups with separate chat/game/headset outputs.
- Transparent always-on-top caption overlay.
- First-run setup wizard for legal consent, microphone selection, and output-device selection.
- Hardware-aware model selection with NVIDIA GPU acceleration when available and CPU fallback when needed.
- Batch re-transcription and speaker diarization tools for improving saved transcripts.
- Installer-based Windows setup with no Python install required.

## Install

Download `TachyonTranscripts-Setup-0.1.2.exe` from the release assets and run it.

The installer is currently unsigned, so Windows SmartScreen may show a warning. Click **More info** -> **Run anyway** if you trust the download.

## Notes

- First launch may take a while if the Whisper model has not been downloaded yet.
- The app requires Windows 10/11.
- NVIDIA GPU is recommended for best real-time performance, but CPU fallback is supported.
- Recording laws vary by location. Read the legal notice before using the app.

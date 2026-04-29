from __future__ import annotations

import json
from pathlib import Path

from tachyon.ui.reviewer import discover_sessions


def test_discover_sessions_uses_manifest_loopback_files(tmp_path: Path) -> None:
    session_dir = tmp_path / "2026-04-12_120000"
    audio_dir = session_dir / "audio"
    audio_dir.mkdir(parents=True)

    (audio_dir / "mic.wav").write_bytes(b"")
    (audio_dir / "system_0.wav").write_bytes(b"")
    (session_dir / "transcript.md").write_text("# Meeting Transcript\n", encoding="utf-8")

    (audio_dir / "device_manifest.json").write_text(
        json.dumps(
            {
                "mic": {"file": "mic.wav"},
                "loopback": [{"file": "system_0.wav", "label": "Chat"}],
            }
        ),
        encoding="utf-8",
    )

    sessions = discover_sessions(tmp_path)
    assert len(sessions) == 1
    assert sessions[0].has_mic_wav is True
    assert sessions[0].loopback_files[0]["label"] == "Chat"


def test_discover_sessions_fallback_system_wav(tmp_path: Path) -> None:
    session_dir = tmp_path / "2026-04-12_121500"
    audio_dir = session_dir / "audio"
    audio_dir.mkdir(parents=True)

    (audio_dir / "system.wav").write_bytes(b"")
    (session_dir / "transcript.md").write_text("# Meeting Transcript\n", encoding="utf-8")

    sessions = discover_sessions(tmp_path)
    assert len(sessions) == 1
    assert sessions[0].loopback_files == [{"file": "system.wav"}]


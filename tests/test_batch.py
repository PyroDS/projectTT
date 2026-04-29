from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from tachyon.batch import BatchTranscriber


def test_discover_audio_files_manifest(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()

    (audio_dir / "mic.wav").write_bytes(b"")
    (audio_dir / "system_0.wav").write_bytes(b"")

    (audio_dir / "device_manifest.json").write_text(
        json.dumps(
            {
                "mic": {"file": "mic.wav"},
                "loopback": [{"file": "system_0.wav", "label": "Chat"}],
            }
        ),
        encoding="utf-8",
    )

    mic, loopbacks = BatchTranscriber._discover_audio_files(audio_dir)
    assert mic == audio_dir / "mic.wav"
    assert loopbacks == [(audio_dir / "system_0.wav", "Them")]


def test_normalize_rms_targets_level() -> None:
    audio = np.array([0.1, -0.1, 0.1, -0.1], dtype=np.float32)
    normalized = BatchTranscriber._normalize_rms(audio, target_rms=0.2)
    rms = BatchTranscriber._rms(normalized)
    assert np.isclose(rms, 0.2, atol=1e-3)


def test_text_similarity() -> None:
    assert BatchTranscriber._texts_similar(
        "hello there team", "hello there", threshold=0.5
    )
    assert not BatchTranscriber._texts_similar(
        "alpha beta", "gamma delta", threshold=0.5
    )


def test_parse_session_datetime_fallback(tmp_path: Path) -> None:
    session_dir = tmp_path / "not_a_session_name"
    session_dir.mkdir()
    dt = BatchTranscriber._parse_session_datetime(session_dir)
    assert dt.year >= 2020


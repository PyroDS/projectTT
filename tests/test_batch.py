from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from tachyon.batch import BatchProgress, BatchTranscriber


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


# ---------------------------------------------------------------------------
# Stream-start offsets (manifest timeline_version 2)
# ---------------------------------------------------------------------------

def _write_manifest(audio_dir: Path, data: dict) -> None:
    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / "device_manifest.json").write_text(
        json.dumps(data), encoding="utf-8"
    )


def test_read_start_offsets_v2_manifest(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    _write_manifest(audio_dir, {
        "timeline_version": 2,
        "mic": {"file": "mic.wav", "start_wall_time": 1000.0},
        "loopback": [
            {"file": "system.wav", "start_wall_time": 1000.8},
        ],
    })

    offsets, has_v2 = BatchTranscriber._read_start_offsets(audio_dir)
    assert has_v2 is True
    assert set(offsets) == {"system.wav"}
    assert offsets["system.wav"] == pytest.approx(0.8)


def test_read_start_offsets_legacy_manifest(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    _write_manifest(audio_dir, {
        "mic": {"file": "mic.wav"},
        "loopback": [{"file": "system.wav", "label": ""}],
    })

    offsets, has_v2 = BatchTranscriber._read_start_offsets(audio_dir)
    assert has_v2 is False
    assert offsets == {}


def test_read_start_offsets_missing_manifest(tmp_path: Path) -> None:
    offsets, has_v2 = BatchTranscriber._read_start_offsets(tmp_path / "audio")
    assert has_v2 is False
    assert offsets == {}


def test_read_start_offsets_no_mic_uses_earliest_loopback(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    _write_manifest(audio_dir, {
        "timeline_version": 2,
        "loopback": [
            {"file": "system_0.wav", "start_wall_time": 2000.5},
            {"file": "system_1.wav", "start_wall_time": 2000.0},
        ],
    })

    offsets, has_v2 = BatchTranscriber._read_start_offsets(audio_dir)
    assert has_v2 is True
    assert offsets == {"system_0.wav": 0.5, "system_1.wav": 0.0}


def test_apply_start_offset_positive_prepends_silence() -> None:
    audio = np.ones(100, dtype=np.float32)
    shifted = BatchTranscriber._apply_start_offset(audio, 0.5, sr=16_000)
    assert len(shifted) == 100 + 8_000
    assert not shifted[:8_000].any()
    assert shifted[8_000:].all()


def test_apply_start_offset_negative_trims() -> None:
    audio = np.arange(16_000, dtype=np.float32)
    shifted = BatchTranscriber._apply_start_offset(audio, -0.25, sr=16_000)
    assert len(shifted) == 12_000
    assert shifted[0] == 4_000.0


def test_apply_start_offset_zero_is_identity() -> None:
    audio = np.ones(10, dtype=np.float32)
    assert BatchTranscriber._apply_start_offset(audio, 0.0) is audio


# ---------------------------------------------------------------------------
# Legacy alignment warning
# ---------------------------------------------------------------------------

def test_legacy_alignment_warning_fires_on_compressed_loopback() -> None:
    msg = BatchTranscriber._legacy_alignment_warning(
        mic_duration_sec=600.0,
        loopback_durations={"system.wav": 532.0},
        has_wall_clock_manifest=False,
    )
    assert msg is not None
    assert "68s" in msg
    assert "system.wav" in msg


def test_legacy_alignment_warning_silent_for_v2_manifest() -> None:
    msg = BatchTranscriber._legacy_alignment_warning(
        mic_duration_sec=600.0,
        loopback_durations={"system.wav": 532.0},
        has_wall_clock_manifest=True,
    )
    assert msg is None


def test_legacy_alignment_warning_silent_within_tolerance() -> None:
    msg = BatchTranscriber._legacy_alignment_warning(
        mic_duration_sec=600.0,
        loopback_durations={"system.wav": 597.0},
        has_wall_clock_manifest=False,
    )
    assert msg is None


def test_legacy_alignment_warning_silent_without_mic() -> None:
    msg = BatchTranscriber._legacy_alignment_warning(
        mic_duration_sec=0.0,
        loopback_durations={"system.wav": 100.0},
        has_wall_clock_manifest=False,
    )
    assert msg is None


def test_batch_progress_warning_propagates() -> None:
    received: list[BatchProgress] = []
    transcriber = BatchTranscriber(
        model=object(), on_progress=received.append,
    )
    transcriber._report("Loading audio", 12, warning="timeline compressed")

    assert len(received) == 1
    assert received[0].warning == "timeline compressed"
    assert received[0].phase == "Loading audio"


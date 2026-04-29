from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from tachyon.diarizer import Diarizer
from tachyon.session import TranscriptSegment


def test_discover_loopback_wavs_from_manifest(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir(parents=True)
    (audio_dir / "system_0.wav").write_bytes(b"")
    (audio_dir / "system_1.wav").write_bytes(b"")

    (audio_dir / "device_manifest.json").write_text(
        json.dumps(
            {
                "loopback": [
                    {"file": "system_0.wav", "label": "Chat"},
                    {"file": "system_1.wav", "label": "Game"},
                ]
            }
        ),
        encoding="utf-8",
    )

    loopbacks = Diarizer._discover_loopback_wavs(audio_dir)
    assert loopbacks == [
        (audio_dir / "system_0.wav", "Chat"),
        (audio_dir / "system_1.wav", "Game"),
    ]


def test_build_speaker_timeline_uses_subsecond_resolution() -> None:
    timeline = Diarizer._build_speaker_timeline(
        window_centers=[0.5, 1.5],
        labels=np.array([0, 1], dtype=np.int32),
        window_sec=1.0,
        audio_duration=2.0,
        resolution_sec=0.25,
    )
    assert timeline[0] == 0
    assert timeline[4] == 1  # 4 * 0.25s = 1.0s


def test_relabel_from_timeline_uses_bin_votes() -> None:
    diarizer = Diarizer()
    segments = [
        TranscriptSegment("Them", "first", 0.0, 0.5),
        TranscriptSegment("You", "me", 0.5, 0.75),
        TranscriptSegment("Them", "second", 0.75, 1.25),
    ]
    # timeline bins at 0.25s: 0-2 -> cluster 10, 3-5 -> cluster 20
    timeline = {
        0: 10, 1: 10, 2: 10,
        3: 20, 4: 20, 5: 20,
    }
    labels = np.array([10, 20], dtype=np.int32)

    relabeled = diarizer._relabel_from_timeline(
        segments,
        timeline,
        labels,
        resolution_sec=0.25,
    )

    assert relabeled[0].speaker == "Speaker 1"
    assert relabeled[1].speaker == "You"
    assert relabeled[2].speaker == "Speaker 2"

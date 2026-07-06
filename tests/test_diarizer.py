from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from tachyon.diarizer import (
    DiarizeConfig,
    Diarizer,
    PyannoteAccessError,
    _is_pyannote_access_error,
    _load_pyannote_pinned,
    _pyannote_access_message,
    _torch_load_legacy_pickle_for_pinned_pyannote,
)
from tachyon.model_pins import (
    HF_TOKEN_SETTINGS_URL,
    PYANNOTE_EMBEDDING_REPO,
    PYANNOTE_EMBEDDING_REVISION,
    PYANNOTE_EMBEDDING_URL,
)
from tachyon.session import TranscriptSegment, WordTiming


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


def test_discover_mic_wav_from_manifest(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir(parents=True)
    (audio_dir / "mic.wav").write_bytes(b"")

    (audio_dir / "device_manifest.json").write_text(
        json.dumps({"mic": {"file": "mic.wav"}}),
        encoding="utf-8",
    )

    mic = Diarizer._discover_mic_wav(audio_dir)
    assert mic == audio_dir / "mic.wav"


def test_resolve_audio_sources_auto_prefers_mixed_when_few_them_segments(
    tmp_path: Path,
) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir(parents=True)
    (audio_dir / "mic.wav").write_bytes(b"")
    (audio_dir / "system.wav").write_bytes(b"")

    diarizer = Diarizer(config=DiarizeConfig(audio_mode="auto"))
    segments = [
        TranscriptSegment("You", "hello", 0.0, 1.0),
        TranscriptSegment("You", "world", 1.0, 2.0),
        TranscriptSegment("Them", "thanks", 2.0, 3.0),
    ]

    resolved = diarizer._resolve_audio_sources(audio_dir, segments)
    assert resolved is not None
    wavs, mode = resolved
    assert mode == "mixed"
    assert wavs == [(audio_dir / "mic.wav", "mic")]


def test_resolve_audio_sources_auto_uses_system_when_enough_them_segments(
    tmp_path: Path,
) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir(parents=True)
    (audio_dir / "mic.wav").write_bytes(b"")
    (audio_dir / "system.wav").write_bytes(b"")

    diarizer = Diarizer(config=DiarizeConfig(audio_mode="auto"))
    segments = [
        TranscriptSegment("You", "hello", 0.0, 1.0),
        TranscriptSegment("Them", "one", 1.0, 2.0),
        TranscriptSegment("Them", "two", 2.0, 3.0),
    ]

    resolved = diarizer._resolve_audio_sources(audio_dir, segments)
    assert resolved is not None
    wavs, mode = resolved
    assert mode == "system"
    assert wavs == [(audio_dir / "system.wav", "")]


def test_resolve_audio_sources_mixed_mode(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir(parents=True)
    (audio_dir / "mic.wav").write_bytes(b"")

    diarizer = Diarizer(config=DiarizeConfig(audio_mode="mixed"))
    segments = [TranscriptSegment("You", "only speaker label", 0.0, 1.0)]

    resolved = diarizer._resolve_audio_sources(audio_dir, segments)
    assert resolved is not None
    wavs, mode = resolved
    assert mode == "mixed"
    assert wavs == [(audio_dir / "mic.wav", "mic")]


def test_legacy_pyannote_backend_rejects_pyannote_4(monkeypatch: pytest.MonkeyPatch) -> None:
    import tachyon.diarization.community_runtime as runtime

    monkeypatch.setattr(runtime, "get_pyannote_major_version", lambda: 4)
    diarizer = Diarizer(config=DiarizeConfig(backend="pyannote", hf_token="hf_test"))

    with pytest.raises(RuntimeError, match="select pyannote_community"):
        diarizer._get_encoder()


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


def test_relabel_from_timeline_system_mode_preserves_you() -> None:
    diarizer = Diarizer()
    segments = [
        TranscriptSegment("Them", "first", 0.0, 0.5),
        TranscriptSegment("You", "me", 0.5, 0.75),
        TranscriptSegment("Them", "second", 0.75, 1.25),
    ]
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
        preserve_you=True,
    )

    assert relabeled[0].speaker == "Speaker 1"
    assert relabeled[1].speaker == "You"
    assert relabeled[2].speaker == "Speaker 2"


def test_relabel_from_timeline_mixed_mode_relabels_you() -> None:
    diarizer = Diarizer()
    segments = [
        TranscriptSegment("You", "first", 0.0, 0.5),
        TranscriptSegment("You", "second", 0.75, 1.25),
    ]
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
        preserve_you=False,
    )

    assert relabeled[0].speaker == "Speaker 1"
    assert relabeled[1].speaker == "Speaker 2"


def test_relabel_segment_by_words_splits_on_speaker_change() -> None:
    diarizer = Diarizer()
    segment = TranscriptSegment(
        speaker="Them",
        text="hello there goodbye",
        start_time=0.0,
        end_time=1.25,
        words=[
            WordTiming("hello ", 0.0, 0.4),
            WordTiming("there ", 0.4, 0.8),
            WordTiming("goodbye", 1.0, 1.25),
        ],
    )
    timeline = {i: 10 for i in range(4)}
    timeline.update({i: 20 for i in range(4, 8)})
    labels = np.array([10, 20], dtype=np.int32)
    cluster_map = Diarizer._build_cluster_to_speaker_map(labels)

    split = diarizer._relabel_segment_by_words(
        segment, timeline, cluster_map, resolution_sec=0.25,
    )

    assert len(split) == 2
    assert split[0].speaker == "Speaker 1"
    assert "hello" in split[0].text
    assert split[1].speaker == "Speaker 2"
    assert "goodbye" in split[1].text


def test_relabel_from_timeline_without_words_keeps_majority_vote() -> None:
    diarizer = Diarizer()
    segments = [
        TranscriptSegment("Them", "mixed block", 0.0, 1.0),
    ]
    timeline = {
        0: 10, 1: 10, 2: 10,
        3: 20, 4: 20,
    }
    labels = np.array([10, 20], dtype=np.int32)

    relabeled = diarizer._relabel_from_timeline(
        segments,
        timeline,
        labels,
        resolution_sec=0.25,
        preserve_you=False,
    )

    assert len(relabeled) == 1
    assert relabeled[0].speaker == "Speaker 1"


def test_pyannote_access_message_includes_hf_urls() -> None:
    message = _pyannote_access_message()
    assert PYANNOTE_EMBEDDING_URL in message
    assert HF_TOKEN_SETTINGS_URL in message


@pytest.mark.parametrize(
    "error_text",
    [
        "You are trying to access a gated repo",
        "401 Client Error: Unauthorized",
        "Invalid user token",
    ],
)
def test_is_pyannote_access_error_detects_hf_access_failures(error_text: str) -> None:
    assert _is_pyannote_access_error(RuntimeError(error_text)) is True


def test_is_pyannote_access_error_ignores_unrelated_failures() -> None:
    assert _is_pyannote_access_error(RuntimeError("CUDA out of memory")) is False


def test_is_pyannote_access_error_ignores_torch_weights_only_error() -> None:
    message = (
        "Weights only load failed. Check the documentation of torch.load "
        "to learn more about types accepted by default."
    )
    assert _is_pyannote_access_error(RuntimeError(message)) is False


def test_torch_load_legacy_context_overrides_none_weights_only(monkeypatch: pytest.MonkeyPatch) -> None:
    import torch

    calls: list[dict] = []

    def fake_torch_load(*_args: object, **kwargs: object) -> object:
        calls.append(dict(kwargs))
        return object()

    monkeypatch.setattr(torch, "load", fake_torch_load)

    with _torch_load_legacy_pickle_for_pinned_pyannote():
        torch.load("checkpoint.bin", weights_only=None)

    assert calls == [{"weights_only": False}]


def test_load_pyannote_pinned_raises_when_model_is_none() -> None:
    model_cls = MagicMock()
    model_cls.from_pretrained.return_value = None

    with pytest.raises(PyannoteAccessError, match="Pyannote model access failed"):
        _load_pyannote_pinned(model_cls, "hf_test_token")

    model_cls.from_pretrained.assert_called_once_with(
        f"{PYANNOTE_EMBEDDING_REPO}@{PYANNOTE_EMBEDDING_REVISION}",
        use_auth_token="hf_test_token",
    )


def test_load_pyannote_pinned_raises_on_access_exception() -> None:
    model_cls = MagicMock()
    model_cls.from_pretrained.side_effect = RuntimeError(
        "401 Client Error: Unauthorized for url"
    )

    with pytest.raises(PyannoteAccessError, match=PYANNOTE_EMBEDDING_URL):
        _load_pyannote_pinned(model_cls, "hf_test_token")

    model_cls.from_pretrained.assert_called_once_with(
        f"{PYANNOTE_EMBEDDING_REPO}@{PYANNOTE_EMBEDDING_REVISION}",
        use_auth_token="hf_test_token",
    )


# ---------------------------------------------------------------------------
# Consecutive-segment merging (gap guard + block cap)
# ---------------------------------------------------------------------------

def test_merge_adjacent_same_speaker_segments() -> None:
    segments = [
        TranscriptSegment("Speaker 1", "hello", 0.0, 2.0),
        TranscriptSegment("Speaker 1", "there", 2.5, 4.0),
    ]
    merged = Diarizer._merge_consecutive_segments(segments)
    assert len(merged) == 1
    assert merged[0].text == "hello there"
    assert merged[0].start_time == 0.0
    assert merged[0].end_time == 4.0


def test_merge_respects_gap_limit() -> None:
    segments = [
        TranscriptSegment("Speaker 1", "hello", 0.0, 2.0),
        TranscriptSegment("Speaker 1", "again", 12.0, 14.0),  # 10 s gap
    ]
    merged = Diarizer._merge_consecutive_segments(segments, max_gap_sec=3.0)
    assert len(merged) == 2
    assert merged[1].start_time == 12.0


def test_merge_respects_block_duration_cap() -> None:
    # Contiguous same-speaker chain spanning 120 s must split at the cap.
    segments = [
        TranscriptSegment("Speaker 1", "part1", 0.0, 50.0),
        TranscriptSegment("Speaker 1", "part2", 50.0, 80.0),
        TranscriptSegment("Speaker 1", "part3", 80.0, 120.0),
    ]
    merged = Diarizer._merge_consecutive_segments(
        segments, max_gap_sec=3.0, max_block_sec=90.0,
    )
    assert len(merged) == 2
    assert merged[0].text == "part1 part2"
    assert merged[0].end_time == 80.0
    assert merged[1].start_time == 80.0


def test_merge_never_joins_different_speakers() -> None:
    segments = [
        TranscriptSegment("Speaker 1", "question", 0.0, 2.0),
        TranscriptSegment("Speaker 2", "answer", 2.0, 4.0),
        TranscriptSegment("Speaker 1", "reply", 4.0, 6.0),
    ]
    merged = Diarizer._merge_consecutive_segments(segments)
    assert len(merged) == 3


def test_merge_concatenates_word_timings() -> None:
    segments = [
        TranscriptSegment("Speaker 1", "hello", 0.0, 1.0,
                          words=[WordTiming("hello", 0.0, 1.0)]),
        TranscriptSegment("Speaker 1", "there", 1.5, 2.5,
                          words=[WordTiming("there", 1.5, 2.5)]),
    ]
    merged = Diarizer._merge_consecutive_segments(segments)
    assert len(merged) == 1
    assert [w.text for w in merged[0].words] == ["hello", "there"]


def test_merge_handles_degenerate_end_times() -> None:
    # Transcripts loaded from markdown can have end_time == start_time
    # (or 0.0) — gap math must not merge across a huge span because of it.
    segments = [
        TranscriptSegment("Speaker 1", "early", 10.0, 0.0),
        TranscriptSegment("Speaker 1", "late", 100.0, 102.0),
    ]
    merged = Diarizer._merge_consecutive_segments(segments, max_gap_sec=3.0)
    assert len(merged) == 2

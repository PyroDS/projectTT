from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import soundfile as sf

from tachyon.diarization.align import align_segments_to_turns, build_speaker_label_map
from tachyon.diarization.community import parse_community_output, run_community_diarization
from tachyon.diarization.community_runtime import (
    community_runtime_issues,
    get_pyannote_major_version,
    load_community_pipeline,
    reset_pyannote_import_state,
    run_community_pipeline,
)
from tachyon.diarization.sources import build_canonical_mix, resolve_community_audio_plan
from tachyon.diarization.types import SpeakerTurn
from tachyon.diarizer import DiarizeConfig
from tachyon.session import TranscriptSegment, WordTiming


class _FakeSegment:
    def __init__(self, start: float, end: float) -> None:
        self.start = start
        self.end = end


class _FakeAnnotation:
    def __init__(self, items: list[tuple[float, float, str]]) -> None:
        self._items = [
            (_FakeSegment(start, end), None, speaker)
            for start, end, speaker in items
        ]

    def itertracks(self, yield_label: bool = False):
        for segment, track, speaker in self._items:
            yield segment, track, speaker


def test_parse_community_output_prefers_exclusive() -> None:
    output = SimpleNamespace(
        exclusive_speaker_diarization=_FakeAnnotation([
            (0.0, 0.8, "SPEAKER_00"),
            (0.8, 1.6, "SPEAKER_01"),
        ]),
        speaker_diarization=_FakeAnnotation([
            (0.0, 1.6, "SPEAKER_00"),
        ]),
    )

    turns = parse_community_output(output)
    assert len(turns) == 2
    assert turns[0].speaker_id == "SPEAKER_00"
    assert turns[1].speaker_id == "SPEAKER_01"


def test_build_speaker_label_map_orders_by_first_appearance() -> None:
    turns = [
        SpeakerTurn("SPEAKER_01", 1.0, 2.0),
        SpeakerTurn("SPEAKER_00", 0.0, 0.8),
        SpeakerTurn("SPEAKER_01", 2.0, 3.0),
    ]
    label_map = build_speaker_label_map(turns)
    assert label_map["SPEAKER_00"] == "Speaker 1"
    assert label_map["SPEAKER_01"] == "Speaker 2"


def test_align_segments_to_turns_splits_words_by_speaker() -> None:
    turns = [
        SpeakerTurn("A", 0.0, 0.7),
        SpeakerTurn("B", 0.7, 1.5),
    ]
    segments = [
        TranscriptSegment(
            "Them",
            "hello there",
            0.0,
            1.4,
            words=[
                WordTiming("hello ", 0.0, 0.5),
                WordTiming("there", 0.8, 1.2),
            ],
        ),
    ]

    relabeled = align_segments_to_turns(segments, turns, preserve_you=False)
    assert len(relabeled) == 2
    assert relabeled[0].speaker == "Speaker 1"
    assert relabeled[1].speaker == "Speaker 2"


def test_build_canonical_mix_averages_channels(tmp_path: Path) -> None:
    left = tmp_path / "left.wav"
    right = tmp_path / "right.wav"
    sf.write(str(left), np.array([0.5, 0.5], dtype=np.float32), 16_000)
    sf.write(str(right), np.array([0.0, 1.0], dtype=np.float32), 16_000)

    mixed = build_canonical_mix([left, right])
    assert mixed is not None
    assert mixed.shape[0] == 2
    assert float(np.max(np.abs(mixed))) > 0.0


def test_resolve_community_audio_plan_builds_mix_for_mic_and_system(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir(parents=True)
    sf.write(str(audio_dir / "mic.wav"), np.zeros(1600, dtype=np.float32), 16_000)
    sf.write(str(audio_dir / "system.wav"), np.ones(1600, dtype=np.float32) * 0.1, 16_000)

    segments = [
        TranscriptSegment("You", "hi", 0.0, 0.5),
        TranscriptSegment("Them", "hello", 0.5, 1.0),
        TranscriptSegment("Them", "world", 1.0, 1.5),
    ]
    plan = resolve_community_audio_plan(audio_dir, segments, audio_mode="mixed")
    assert plan is not None
    assert plan.wav_path.exists()
    assert plan.temp_file is True
    assert plan.effective_mode == "mixed"


@patch("tachyon.diarization.community.load_community_pipeline")
@patch("tachyon.diarization.community.run_community_pipeline")
def test_run_community_diarization_end_to_end(
    mock_run: MagicMock,
    mock_load: MagicMock,
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    audio_dir = session_dir / "audio"
    audio_dir.mkdir(parents=True)
    sf.write(str(audio_dir / "mic.wav"), np.ones(32000, dtype=np.float32) * 0.05, 16_000)

    transcript = session_dir / "transcript_v2.md"
    transcript.write_text(
        "\n".join([
            "# Meeting Transcript",
            "",
            "**Duration**: 0:00:02",
            "---",
            "",
            "**[0:00:00] Them:**",
            "hello",
            "",
            "**[0:00:01] Them:**",
            "there",
            "",
        ]),
        encoding="utf-8",
    )
    sidecar = {
        "schema_version": 2,
        "duration_sec": 2.0,
        "segments": [
            {
                "id": "seg_0001",
                "speaker": "Them",
                "text": "hello",
                "start": 0.0,
                "end": 0.7,
                "words": [
                    {"text": "hello ", "start": 0.0, "end": 0.7},
                ],
            },
            {
                "id": "seg_0002",
                "speaker": "Them",
                "text": "there",
                "start": 0.8,
                "end": 1.4,
                "words": [
                    {"text": "there", "start": 0.8, "end": 1.4},
                ],
            },
        ],
    }
    transcript.with_suffix(".json").write_text(json.dumps(sidecar), encoding="utf-8")

    mock_load.return_value = MagicMock()
    mock_run.return_value = SimpleNamespace(
        exclusive_speaker_diarization=_FakeAnnotation([
            (0.0, 0.75, "SPEAKER_00"),
            (0.75, 1.5, "SPEAKER_01"),
        ]),
    )

    config = DiarizeConfig(
        backend="pyannote_community",
        hf_token="hf_test",
        audio_mode="mixed",
        num_speakers=2,
    )
    result = run_community_diarization(
        session_dir,
        "transcript_v2.md",
        config,
    )
    assert result is not None
    segments, speaker_infos = result
    assert len(segments) == 2
    assert segments[0].speaker == "Speaker 1"
    assert segments[1].speaker == "Speaker 2"
    assert len(speaker_infos) == 2
    mock_run.assert_called_once()
    _pipeline, audio_arg = mock_run.call_args.args[:2]
    assert isinstance(audio_arg, np.ndarray)


def test_diarizer_routes_to_community_backend(tmp_path: Path) -> None:
    with patch("tachyon.diarization.community.run_community_diarization") as mock_run:
        mock_run.return_value = ([], [])
        diarizer = __import__("tachyon.diarizer", fromlist=["Diarizer"]).Diarizer(
            config=DiarizeConfig(backend="pyannote_community"),
        )
        result = diarizer.diarize_session(tmp_path, "transcript.md")
        mock_run.assert_called_once()
        assert result == ([], [])


def test_load_community_pipeline_passes_revision_keyword(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import tachyon.diarization.community_runtime as runtime

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    expected_pipeline = object()

    class FakePipeline:
        @staticmethod
        def from_pretrained(*args: object, **kwargs: object) -> object:
            calls.append((args, kwargs))
            return expected_pipeline

    pyannote_module = types.ModuleType("pyannote")
    pyannote_audio_module = types.ModuleType("pyannote.audio")
    pyannote_audio_module.Pipeline = FakePipeline
    pyannote_module.audio = pyannote_audio_module

    monkeypatch.setitem(sys.modules, "pyannote", pyannote_module)
    monkeypatch.setitem(sys.modules, "pyannote.audio", pyannote_audio_module)
    monkeypatch.setattr(runtime, "community_runtime_issues", lambda: [])
    monkeypatch.setattr(runtime, "ensure_community_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(runtime, "reset_pyannote_import_state", lambda: None)

    pipeline = load_community_pipeline("hf_test")

    assert pipeline is expected_pipeline
    assert calls == [(
        (runtime.PYANNOTE_COMMUNITY_REPO,),
        {
            "revision": runtime.PYANNOTE_COMMUNITY_REVISION,
            "token": "hf_test",
        },
    )]


def test_run_community_pipeline_passes_preloaded_waveform() -> None:
    captured: dict[str, object] = {}

    def fake_pipeline(input_audio: object, **kwargs: object) -> object:
        captured["input_audio"] = input_audio
        captured["kwargs"] = kwargs
        return "ok"

    audio = np.ones(1600, dtype=np.float32)

    result = run_community_pipeline(
        fake_pipeline,
        audio,
        num_speakers=2,
    )

    assert result == "ok"
    input_audio = captured["input_audio"]
    assert isinstance(input_audio, dict)
    assert input_audio["sample_rate"] == 16_000
    assert tuple(input_audio["waveform"].shape) == (1, 1600)
    assert captured["kwargs"] == {"num_speakers": 2}


def test_community_runtime_issues_without_pyannote(monkeypatch: pytest.MonkeyPatch) -> None:
    import tachyon.diarization.community_runtime as runtime

    monkeypatch.setattr(
        runtime,
        "get_pyannote_major_version",
        lambda: None,
    )

    def fake_find_spec(name: str):
        return None if name == "pyannote.audio" else object()

    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    issues = runtime.community_runtime_issues()
    assert "not installed" in issues[0]


def test_pyannote_version_uses_distribution_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.metadata

    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda package: "4.0.1" if package == "pyannote.audio" else "0.0.0",
    )

    assert get_pyannote_major_version() == 4


def test_reset_pyannote_import_state_clears_cached_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    monkeypatch.setitem(sys.modules, "pyannote", types.ModuleType("pyannote"))
    monkeypatch.setitem(sys.modules, "pyannote.audio", types.ModuleType("pyannote.audio"))

    reset_pyannote_import_state()

    assert "pyannote" not in sys.modules
    assert "pyannote.audio" not in sys.modules

from __future__ import annotations

import queue
import sys
import types
from dataclasses import dataclass

import numpy as np


@dataclass
class _AudioChunkStub:
    source: str
    audio: object
    timestamp: float


capture_stub = types.ModuleType("tachyon.capture")
capture_stub.AudioChunk = _AudioChunkStub
sys.modules.setdefault("tachyon.capture", capture_stub)

from tachyon.transcriber import Transcriber, _classify_runtime_error, _resolve_speaker_label


def test_resolve_speaker_labels() -> None:
    assert _resolve_speaker_label("you") == "You"
    assert _resolve_speaker_label("them") == "Them"
    assert _resolve_speaker_label("them:Chat") == "Them (Chat)"
    assert _resolve_speaker_label("unknown") == "unknown"


def test_classify_runtime_error_missing_cublas() -> None:
    err = RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
    is_fatal, code, hint = _classify_runtime_error(err)
    assert is_fatal is True
    assert code == "cuda_missing_cublas"
    assert "compute_device" in hint


def test_classify_runtime_error_nonfatal_unknown() -> None:
    err = RuntimeError("Some temporary non-CUDA decoder hiccup")
    is_fatal, code, hint = _classify_runtime_error(err)
    assert is_fatal is False
    assert code == ""
    assert "hiccup" in hint


def test_transcriber_uses_configured_beam_size_and_overlap() -> None:
    @dataclass
    class _Word:
        word: str
        start: float
        end: float

    @dataclass
    class _Segment:
        words: list[_Word]

    class _FakeModel:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self._call_idx = 0

        def transcribe(self, combined, **kwargs):  # noqa: ANN001
            self._call_idx += 1
            self.calls.append(
                {
                    "beam_size": kwargs.get("beam_size"),
                    "sample_count": int(len(combined)),
                }
            )
            if self._call_idx == 1:
                words = [_Word(" First", 0.1, 0.2)]
            else:
                # With 0.5s overlap, this keeps only "New".
                words = [_Word(" Drop", 0.2, 0.3), _Word(" New", 0.6, 0.7)]
            return iter([_Segment(words=words)]), None

    emitted = []
    transcriber = Transcriber(
        chunk_queue=queue.Queue(),
        on_segment=emitted.append,
        overlap_sec=0.5,
        beam_size=3,
    )
    transcriber._model = _FakeModel()  # noqa: SLF001
    transcriber.set_session_start_time(100.0)

    first = _AudioChunkStub(
        source="you",
        audio=np.ones(8_000, dtype=np.float32),
        timestamp=100.0,
    )
    second = _AudioChunkStub(
        source="you",
        audio=np.ones(8_000, dtype=np.float32),
        timestamp=100.5,
    )

    transcriber._process_chunk(first)  # noqa: SLF001
    transcriber._process_chunk(second)  # noqa: SLF001

    assert len(emitted) == 2
    assert emitted[0].text == "First"
    assert emitted[1].text == "New"
    assert transcriber._model.calls[0]["beam_size"] == 3  # noqa: SLF001
    assert transcriber._model.calls[1]["sample_count"] == 16_000  # noqa: SLF001


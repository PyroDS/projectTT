from __future__ import annotations

import sys
import types
from dataclasses import dataclass


@dataclass
class _AudioChunkStub:
    source: str
    audio: object
    timestamp: float


capture_stub = types.ModuleType("tachyon.capture")
capture_stub.AudioChunk = _AudioChunkStub
sys.modules.setdefault("tachyon.capture", capture_stub)

from tachyon.transcriber import _classify_runtime_error, _resolve_speaker_label


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


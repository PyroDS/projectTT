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

from tachyon.transcriber import _resolve_speaker_label


def test_resolve_speaker_labels() -> None:
    assert _resolve_speaker_label("you") == "You"
    assert _resolve_speaker_label("them") == "Them"
    assert _resolve_speaker_label("them:Chat") == "Them (Chat)"
    assert _resolve_speaker_label("unknown") == "unknown"


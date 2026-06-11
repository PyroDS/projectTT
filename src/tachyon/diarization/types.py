"""Shared types for modular diarization backends."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SpeakerTurn:
    """A contiguous speaker-active interval from a diarization pipeline."""

    speaker_id: str
    start: float
    end: float


@dataclass(frozen=True)
class DiarizeAudioPlan:
    """Resolved audio input for a diarization run."""

    wav_path: Path
    effective_mode: str
    preserve_you: bool
    temp_file: bool = False

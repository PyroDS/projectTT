"""Modular speaker diarization backends.

The embedding-clustering path lives in :mod:`tachyon.diarizer`.
The high-accuracy Community-1 pipeline lives under this package.
"""

from tachyon.diarization.community import run_community_diarization
from tachyon.diarization.types import DiarizeAudioPlan, SpeakerTurn

__all__ = [
    "DiarizeAudioPlan",
    "SpeakerTurn",
    "run_community_diarization",
]

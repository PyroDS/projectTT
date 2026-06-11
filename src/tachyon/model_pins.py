"""Pinned HuggingFace model revisions.

Every Whisper, speaker-embedding, and diarization model the app loads
goes through HuggingFace Hub.  HF model loading is a code-execution
boundary -- malicious model weights can run arbitrary code on load --
so we pin each model to a specific commit SHA rather than letting HF
serve us whatever is at HEAD.  A compromised HF account or registry
incident cannot swap the weights out from under us without us bumping
the pin here first.

To refresh a pin:
    1. Visit https://huggingface.co/<repo>/commits/main
    2. Verify the latest commit is from a known-good author.
    3. Copy the commit SHA into the constant below.
    4. Smoke-test loading the model locally before committing.
"""

from __future__ import annotations

from typing import Optional


# ---------------------------------------------------------------------------
# Whisper transcription models (Systran's CTranslate2-converted repos).
# Keys are the model_size strings accepted by faster-whisper's WhisperModel.
# ---------------------------------------------------------------------------
WHISPER_REVISIONS: dict[str, str] = {
    # Systran/faster-whisper-large-v3
    "large-v3":         "edaa852ec7e145841d8ffdb056a99866b5f0a478",
    # Systran/faster-whisper-medium
    "medium":           "08e178d48790749d25932bbc082711ddcfdfbc4f",
    # Systran/faster-whisper-small
    "small":            "536b0662742c02347bc0e980a01041f333bce120",
    # Systran/faster-distil-whisper-large-v3
    "distil-large-v3":  "c3058b475261292e64a0412df1d2681c06260fab",
}


def whisper_revision(model_size: str) -> Optional[str]:
    """Return the pinned commit SHA for a Whisper model size, or None.

    Unknown / unpinned model sizes return ``None``.  Callers should
    pass the result straight through to ``WhisperModel(..., revision=...)``
    -- ``None`` lets faster-whisper fall back to its default branch
    behavior, so unrecognised model sizes still work but lose the
    pinning guarantee.
    """
    return WHISPER_REVISIONS.get(model_size)


# ---------------------------------------------------------------------------
# Speaker-embedding models used by the diarizer.
# ---------------------------------------------------------------------------

# speechbrain ECAPA-TDNN -- default backend, no token required.
SPEECHBRAIN_ECAPA_REPO: str     = "speechbrain/spkrec-ecapa-voxceleb"
SPEECHBRAIN_ECAPA_REVISION: str = "0f99f2d0ebe89ac095bcc5903c4dd8f72b367286"

# pyannote embedding -- optional backend, requires HF token.
PYANNOTE_EMBEDDING_REPO: str     = "pyannote/embedding"
PYANNOTE_EMBEDDING_REVISION: str = "4db4899737a38b2d618bbd74350915aa10293cb2"
PYANNOTE_EMBEDDING_URL: str      = "https://huggingface.co/pyannote/embedding"
HF_TOKEN_SETTINGS_URL: str       = "https://huggingface.co/settings/tokens"

# pyannote Community-1 full diarization pipeline -- optional high-accuracy backend.
PYANNOTE_COMMUNITY_REPO: str     = "pyannote/speaker-diarization-community-1"
PYANNOTE_COMMUNITY_REVISION: str = "3533c8cf8e369892e6b79ff1bf80f7b0286a54ee"
PYANNOTE_COMMUNITY_URL: str      = "https://huggingface.co/pyannote/speaker-diarization-community-1"

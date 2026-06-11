"""Local runtime management for pyannote Community-1 diarization."""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
import re
import sys
from pathlib import Path
from typing import Any, Optional

import numpy as np

from tachyon.config import PROJECT_ROOT
from tachyon.diarizer import PyannoteAccessError
from tachyon.model_pins import (
    HF_TOKEN_SETTINGS_URL,
    PYANNOTE_COMMUNITY_REPO,
    PYANNOTE_COMMUNITY_REVISION,
    PYANNOTE_COMMUNITY_URL,
)

logger = logging.getLogger(__name__)

_COMMUNITY_CACHE_DIR: Path = PROJECT_ROOT / "models" / "pyannote-community-1"
_MIN_COMMUNITY_MAJOR: int = 4


def community_access_message() -> str:
    return (
        "Community-1 model access failed. Accept model terms at "
        f"{PYANNOTE_COMMUNITY_URL}, verify your HuggingFace token at "
        f"{HF_TOKEN_SETTINGS_URL}, or switch to SpeechBrain."
    )


def _is_access_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    markers = (
        "private or gated",
        "gated",
        "unauthorized",
        "authentication",
        "access token",
        "permission",
        "credentials",
        "user conditions",
        "token",
        "401",
        "403",
        "cannot find an appropriate cached snapshot",
        "repository not found",
    )
    return any(marker in msg for marker in markers)


def get_pyannote_major_version() -> Optional[int]:
    """Return the installed pyannote.audio major version, if available.

    Uses distribution metadata rather than importing ``pyannote.audio``.
    During on-demand upgrades, the running app can keep the old 3.x module
    cached in ``sys.modules`` after pip installs 4.x.
    """
    try:
        version = importlib.metadata.version("pyannote.audio")
    except importlib.metadata.PackageNotFoundError:
        return None
    match = re.match(r"(\d+)", version)
    if match:
        return int(match.group(1))
    return None


def reset_pyannote_import_state() -> None:
    """Clear cached pyannote modules after an on-demand pip install."""
    importlib.invalidate_caches()
    for module_name in list(sys.modules):
        if module_name == "pyannote" or module_name.startswith("pyannote."):
            sys.modules.pop(module_name, None)


def community_runtime_issues() -> list[str]:
    """Return human-readable setup issues for the Community-1 backend."""
    issues: list[str] = []
    try:
        import importlib.util
        found = importlib.util.find_spec("pyannote.audio") is not None
    except (ModuleNotFoundError, ValueError):
        found = False

    if not found:
        issues.append("pyannote.audio is not installed")
        return issues

    major = get_pyannote_major_version()
    if major is None:
        issues.append("Could not determine pyannote.audio version")
    elif major < _MIN_COMMUNITY_MAJOR:
        issues.append(
            f"pyannote.audio {major}.x is installed; Community-1 requires 4.x",
        )
    return issues


def ensure_community_cache_dir() -> Path:
    _COMMUNITY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _COMMUNITY_CACHE_DIR


def load_community_pipeline(hf_token: str) -> Any:
    """Load the pinned Community-1 pipeline and cache it locally."""
    runtime_issues = community_runtime_issues()
    if runtime_issues:
        raise RuntimeError("; ".join(runtime_issues))

    if not hf_token:
        raise PyannoteAccessError(
            "Community-1 requires a HuggingFace token. "
            "Set hf_token in config.json or enter it when prompted.",
        )

    ensure_community_cache_dir()
    reset_pyannote_import_state()

    try:
        from pyannote.audio import Pipeline  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError("pyannote.audio is not installed") from exc

    try:
        try:
            pipeline = Pipeline.from_pretrained(
                PYANNOTE_COMMUNITY_REPO,
                revision=PYANNOTE_COMMUNITY_REVISION,
                token=hf_token,
            )
        except TypeError:
            pipeline = Pipeline.from_pretrained(
                PYANNOTE_COMMUNITY_REPO,
                revision=PYANNOTE_COMMUNITY_REVISION,
                use_auth_token=hf_token,
            )
    except Exception as exc:
        if _is_access_error(exc):
            raise PyannoteAccessError(community_access_message()) from exc
        raise

    if pipeline is None:
        raise PyannoteAccessError(community_access_message())

    logger.info(
        "Loaded Community-1 pipeline from %s at %s",
        PYANNOTE_COMMUNITY_REPO,
        PYANNOTE_COMMUNITY_REVISION,
    )
    return pipeline


def run_community_pipeline(
    pipeline: Any,
    audio: np.ndarray,
    sample_rate: int = 16_000,
    num_speakers: Optional[int] = None,
    min_speakers: Optional[int] = None,
    max_speakers: Optional[int] = None,
) -> Any:
    """Run Community-1 diarization on preloaded mono audio."""
    kwargs: dict[str, Any] = {}
    if num_speakers is not None:
        kwargs["num_speakers"] = num_speakers
    else:
        if min_speakers is not None:
            kwargs["min_speakers"] = min_speakers
        if max_speakers is not None:
            kwargs["max_speakers"] = max_speakers

    import torch

    waveform = torch.from_numpy(audio).unsqueeze(0).float()
    pipeline_input = {"waveform": waveform, "sample_rate": sample_rate}
    try:
        return pipeline(pipeline_input, **kwargs)
    except TypeError:
        return pipeline(pipeline_input)

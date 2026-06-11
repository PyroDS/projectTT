"""Audio source resolution and canonical mix creation for diarization."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf
import soxr

from tachyon.diarization.types import DiarizeAudioPlan
from tachyon.session import TranscriptSegment

logger = logging.getLogger(__name__)

TARGET_SAMPLERATE: int = 16_000
_CANONICAL_MIX_NAME: str = "_diarize_canonical.wav"


def discover_loopback_wavs(audio_dir: Path) -> list[tuple[Path, str]]:
    """Discover loopback WAV files from device_manifest.json or fallback."""
    import json

    manifest_path = audio_dir / "device_manifest.json"
    if manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            result: list[tuple[Path, str]] = []
            for lb in data.get("loopback", []):
                lb_file = lb.get("file")
                if lb_file:
                    path = audio_dir / lb_file
                    if path.exists():
                        result.append((path, lb.get("label", "")))
            if result:
                return result
        except Exception:
            logger.warning("Failed reading device manifest", exc_info=True)

    system_wav = audio_dir / "system.wav"
    if system_wav.exists():
        return [(system_wav, "")]
    return []


def discover_mic_wav(
    audio_dir: Path,
    audio_file: Optional[str] = None,
) -> Optional[Path]:
    """Discover the mic / mixed WAV file from manifest or fallback."""
    import json

    if audio_file:
        path = audio_dir / audio_file
        return path if path.exists() else None

    manifest_path = audio_dir / "device_manifest.json"
    if manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            mic_file = data.get("mic", {}).get("file")
            if mic_file:
                path = audio_dir / mic_file
                if path.exists():
                    return path
        except Exception:
            pass

    mic_wav = audio_dir / "mic.wav"
    return mic_wav if mic_wav.exists() else None


def load_mono_audio(path: Path, target_rms: float = 0.05) -> Optional[np.ndarray]:
    """Load a WAV file as 16 kHz mono float32 with RMS normalization."""
    try:
        audio, sr = sf.read(str(path), dtype="float32")
    except Exception:
        logger.exception("Failed to read %s", path)
        return None

    if audio.ndim > 1:
        audio = audio.mean(axis=1).astype(np.float32)
    if sr != TARGET_SAMPLERATE:
        audio = soxr.resample(audio, sr, TARGET_SAMPLERATE).astype(np.float32)

    rms = float(np.sqrt(np.mean(audio ** 2)))
    if rms > 0:
        scale = target_rms / rms
        audio = np.clip(audio * scale, -1.0, 1.0).astype(np.float32)
    return audio


def build_canonical_mix(wav_paths: list[Path], target_rms: float = 0.05) -> Optional[np.ndarray]:
    """Build a normalized mono mix from multiple WAV files aligned to max length."""
    parts: list[np.ndarray] = []
    max_len = 0
    for path in wav_paths:
        audio = load_mono_audio(path, target_rms=target_rms)
        if audio is None or audio.size == 0:
            continue
        parts.append(audio)
        max_len = max(max_len, len(audio))

    if not parts:
        return None

    mixed = np.zeros(max_len, dtype=np.float32)
    for audio in parts:
        mixed[: len(audio)] += audio
    mixed /= float(len(parts))

    rms = float(np.sqrt(np.mean(mixed ** 2)))
    if rms > 0:
        scale = target_rms / rms
        mixed = np.clip(mixed * scale, -1.0, 1.0).astype(np.float32)
    return mixed


def write_temp_wav(audio_dir: Path, audio: np.ndarray, filename: str = _CANONICAL_MIX_NAME) -> Path:
    """Write a temporary mono WAV into the session audio directory."""
    path = audio_dir / filename
    sf.write(str(path), audio, TARGET_SAMPLERATE, subtype="FLOAT")
    return path


def resolve_effective_mode(
    requested_mode: str,
    source_segments: list[TranscriptSegment],
    loopbacks: list[tuple[Path, str]],
    mic_path: Optional[Path],
) -> Optional[str]:
    """Resolve auto/system/mixed into an effective diarization mode."""
    mode = (requested_mode or "auto").strip().lower()
    them_segments = [s for s in source_segments if s.speaker != "You"]

    if mode == "system":
        return "system" if loopbacks else None
    if mode == "mixed":
        return "mixed" if mic_path is not None else None
    if mode == "auto":
        if loopbacks and len(them_segments) >= 2:
            return "system"
        if mic_path is not None and len(source_segments) >= 2:
            return "mixed"
        if loopbacks:
            return "system"
        if mic_path is not None:
            return "mixed"
        return None

    logger.error("Unknown audio_mode: %s", requested_mode)
    return None


def resolve_community_audio_plan(
    audio_dir: Path,
    source_segments: list[TranscriptSegment],
    audio_mode: str,
    audio_file: Optional[str] = None,
    prefer_canonical_mix: bool = True,
) -> Optional[DiarizeAudioPlan]:
    """Resolve the WAV file Community-1 should diarize."""
    loopbacks = discover_loopback_wavs(audio_dir)
    mic_path = discover_mic_wav(audio_dir, audio_file)
    effective_mode = resolve_effective_mode(
        audio_mode, source_segments, loopbacks, mic_path,
    )
    if effective_mode is None:
        return None

    preserve_you = effective_mode == "system"

    if effective_mode == "system":
        if len(loopbacks) == 1:
            return DiarizeAudioPlan(
                wav_path=loopbacks[0][0],
                effective_mode=effective_mode,
                preserve_you=preserve_you,
            )
        loopback_paths = [path for path, _ in loopbacks]
        mixed = build_canonical_mix(loopback_paths)
        if mixed is None:
            return None
        temp_path = write_temp_wav(audio_dir, mixed, "_diarize_system_mix.wav")
        return DiarizeAudioPlan(
            wav_path=temp_path,
            effective_mode=effective_mode,
            preserve_you=preserve_you,
            temp_file=True,
        )

    # mixed mode — prefer a canonical mix when both mic and loopback exist.
    if prefer_canonical_mix and mic_path is not None and loopbacks:
        mix_paths = [mic_path] + [path for path, _ in loopbacks]
        mixed = build_canonical_mix(mix_paths)
        if mixed is not None:
            temp_path = write_temp_wav(audio_dir, mixed)
            return DiarizeAudioPlan(
                wav_path=temp_path,
                effective_mode="mixed",
                preserve_you=False,
                temp_file=True,
            )

    if mic_path is not None:
        return DiarizeAudioPlan(
            wav_path=mic_path,
            effective_mode="mixed",
            preserve_you=False,
        )

    if loopbacks:
        if len(loopbacks) == 1:
            return DiarizeAudioPlan(
                wav_path=loopbacks[0][0],
                effective_mode="mixed",
                preserve_you=False,
            )
        mixed = build_canonical_mix([path for path, _ in loopbacks])
        if mixed is None:
            return None
        temp_path = write_temp_wav(audio_dir, mixed)
        return DiarizeAudioPlan(
            wav_path=temp_path,
            effective_mode="mixed",
            preserve_you=False,
            temp_file=True,
        )

    return None

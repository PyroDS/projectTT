"""Batch re-transcription engine for post-recording processing.

Re-processes saved mic.wav and system.wav files with higher-quality settings
than real-time transcription allows:
  - Full file processing (no 3-second chunking)
  - Beam search (beam_size=5) instead of greedy decoding
  - VAD-based segmentation via Silero
  - condition_on_previous_text for cross-segment coherence
  - Crosstalk suppression between mic and system channels
  - Deduplication of bleed-through segments

Shares the same WhisperModel instance as the real-time transcriber to avoid
duplicating GPU VRAM usage.  Must not run concurrently with real-time
transcription (enforced at the UI level).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import numpy as np
import soundfile as sf
import soxr

from tachyon.session import TranscriptSegment
from tachyon.exporter import (
    export_transcript_versioned,
    next_version_number,
)

logger = logging.getLogger(__name__)

TARGET_SAMPLERATE: int = 16_000

# Regex to extract session start datetime from folder name
_SESSION_DIR_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{6})$")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class BatchConfig:
    """Configuration for batch re-transcription.

    Attributes:
        beam_size: Beam search width (higher = slower but more accurate).
        target_rms: Target RMS level for audio normalization before
            transcription.  Both channels are normalized to this level
            to ensure consistent VAD behavior regardless of mic gain.
        vad_threshold: Silero VAD speech detection threshold (0.0-1.0).
            Lower = more sensitive.  Default 0.2 catches quiet mic speech
            that the default 0.5 misses.
        vad_min_silence_ms: Minimum silence duration in ms before VAD
            considers a speech segment ended.
        vad_speech_pad_ms: Padding in ms added before and after each
            detected speech region.
        energy_threshold: RMS energy ratio below which a segment is
            considered bleed-through and suppressed.  E.g. 0.10 means
            a segment is suppressed if its normalized channel energy is
            < 10% of the other channel's normalized energy during overlap.
        dedup_time_tolerance: Maximum time overlap in seconds for two
            segments to be considered duplicates.
        dedup_text_similarity: Minimum ratio of shared words for two
            segments to be considered duplicates (0.0 to 1.0).
    """

    beam_size: int = 5
    target_rms: float = 0.05
    vad_threshold: float = 0.2
    vad_min_silence_ms: int = 1000
    vad_speech_pad_ms: int = 500
    energy_threshold: float = 0.10
    dedup_time_tolerance: float = 2.0
    dedup_text_similarity: float = 0.6


@dataclass
class BatchProgress:
    """Progress update from the batch transcription process.

    Attributes:
        phase: Human-readable phase name (e.g. "Loading audio",
            "Transcribing mic", "Merging channels").
        percent: Overall completion percentage (0-100).
        detail: Optional detail string for the current operation.
    """

    phase: str
    percent: int
    detail: str = ""


# ---------------------------------------------------------------------------
# BatchTranscriber
# ---------------------------------------------------------------------------

class BatchTranscriber:
    """Re-transcribes a recorded session with enhanced quality settings.

    Parameters
    ----------
    model:
        A loaded ``faster_whisper.WhisperModel`` instance shared with the
        real-time transcriber.  Must not be ``None``.
    config:
        Batch transcription settings.  Uses defaults if not provided.
    on_progress:
        Optional callback invoked with :class:`BatchProgress` updates.
        Called from the batch worker thread — caller must schedule UI
        updates via ``root.after()`` if needed.
    """

    def __init__(
        self,
        model: Any,
        config: Optional[BatchConfig] = None,
        on_progress: Optional[Callable[[BatchProgress], None]] = None,
    ) -> None:
        if model is None:
            raise ValueError("BatchTranscriber requires a loaded WhisperModel")
        self._model = model
        self._config = config or BatchConfig()
        self._on_progress = on_progress

    def _report(self, phase: str, percent: int, detail: str = "") -> None:
        """Emit a progress update."""
        if self._on_progress is not None:
            self._on_progress(BatchProgress(phase=phase, percent=percent, detail=detail))

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def transcribe_session(
        self,
        session_dir: Path,
        stop_event: Optional[Any] = None,
    ) -> list[TranscriptSegment]:
        """Re-transcribe a recorded session from its saved WAV files.

        Reads ``device_manifest.json`` if present to discover all loopback
        WAV files.  Falls back to ``mic.wav``/``system.wav`` for old sessions.

        Parameters
        ----------
        session_dir:
            Path to the session folder containing ``audio/`` directory.
        stop_event:
            Optional ``threading.Event`` checked between operations for
            cancellation support.

        Returns
        -------
        list[TranscriptSegment]
            The re-transcribed segments sorted by start time.

        Raises
        ------
        FileNotFoundError:
            If no audio files are found.
        """
        audio_dir = session_dir / "audio"

        # Discover WAV files via manifest or fallback
        mic_path, loopback_wavs = self._discover_audio_files(audio_dir)

        if mic_path is None and not loopback_wavs:
            raise FileNotFoundError(
                f"No audio files found in {audio_dir}"
            )

        # -- Phase 1: Load and resample audio ------------------------------
        self._report("Loading audio", 5)

        mic_audio: Optional[np.ndarray] = None
        duration_seconds: float = 0.0

        if mic_path is not None and mic_path.exists():
            mic_audio, mic_sr = sf.read(mic_path, dtype="float32")
            if mic_audio.ndim > 1:
                mic_audio = mic_audio.mean(axis=1).astype(np.float32)
            if mic_sr != TARGET_SAMPLERATE:
                mic_audio = soxr.resample(mic_audio, mic_sr, TARGET_SAMPLERATE).astype(np.float32)
            duration_seconds = max(duration_seconds, len(mic_audio) / TARGET_SAMPLERATE)
            logger.info(
                "Loaded %s: %.1fs, RMS=%.6f, peak=%.4f",
                mic_path.name,
                len(mic_audio) / TARGET_SAMPLERATE,
                self._rms(mic_audio),
                float(np.max(np.abs(mic_audio))),
            )

        if self._cancelled(stop_event):
            return []

        # Load all loopback WAV files
        loopback_data: list[tuple[np.ndarray, str]] = []  # (audio, speaker_label)
        for wav_path, speaker_label in loopback_wavs:
            if not wav_path.exists():
                continue
            lb_audio, lb_sr = sf.read(wav_path, dtype="float32")
            if lb_audio.ndim > 1:
                lb_audio = lb_audio.mean(axis=1).astype(np.float32)
            if lb_sr != TARGET_SAMPLERATE:
                lb_audio = soxr.resample(lb_audio, lb_sr, TARGET_SAMPLERATE).astype(np.float32)
            if lb_audio.size == 0:
                logger.warning(
                    "Skipping empty loopback file %s (%s)",
                    wav_path.name, speaker_label,
                )
                continue
            duration_seconds = max(duration_seconds, len(lb_audio) / TARGET_SAMPLERATE)
            logger.info(
                "Loaded %s (%s): %.1fs, RMS=%.6f, peak=%.4f",
                wav_path.name, speaker_label,
                len(lb_audio) / TARGET_SAMPLERATE,
                self._rms(lb_audio),
                float(np.max(np.abs(lb_audio))),
            )
            loopback_data.append((lb_audio, speaker_label))

            if self._cancelled(stop_event):
                return []

        # -- Phase 2: Transcribe mic channel --------------------------------
        mic_segments: list[TranscriptSegment] = []
        if mic_audio is not None and mic_audio.size > 0:
            self._report("Transcribing mic", 15, "Beam search + VAD")
            mic_segments = self._transcribe_channel(
                mic_audio, "You", stop_event,
            )
            logger.info("Mic channel: %d segments", len(mic_segments))

        if self._cancelled(stop_event):
            return []

        # -- Phase 3: Transcribe loopback channel(s) -----------------------
        all_loopback_segments: list[TranscriptSegment] = []
        n_loopbacks = len(loopback_data)
        for i, (lb_audio, lb_speaker) in enumerate(loopback_data):
            if lb_audio.size == 0:
                continue
            # Distribute progress: 50% of total for loopback transcription
            pct = 25 + int(50 * i / max(n_loopbacks, 1))
            self._report(f"Transcribing {lb_speaker}", pct, "Beam search + VAD")
            lb_segments = self._transcribe_channel(
                lb_audio, lb_speaker, stop_event,
            )
            logger.info("Channel %s: %d segments", lb_speaker, len(lb_segments))
            all_loopback_segments.extend(lb_segments)

            if self._cancelled(stop_event):
                return []

        # -- Phase 4: Crosstalk suppression (mic vs each loopback) ---------
        self._report("Suppressing crosstalk", 80)

        if mic_audio is not None:
            for lb_audio, _ in loopback_data:
                mic_segments = self._suppress_crosstalk(
                    mic_segments, mic_audio, lb_audio, is_mic=True,
                )
            for lb_audio, _ in loopback_data:
                # Suppress loopback segments that are bleed from mic
                lb_segs = [s for s in all_loopback_segments
                           if s.speaker != "You"]
                suppressed = self._suppress_crosstalk(
                    lb_segs, lb_audio, mic_audio, is_mic=False,
                )
                suppressed_set = set(id(s) for s in suppressed)
                all_loopback_segments = [
                    s for s in all_loopback_segments
                    if s.speaker == "You" or id(s) in suppressed_set
                ]

        # -- Phase 5: Merge and deduplicate ---------------------------------
        self._report("Merging channels", 90)

        all_segments = mic_segments + all_loopback_segments
        all_segments.sort(key=lambda s: s.start_time)

        # Dedup mic vs all loopback audio (use first loopback for energy comparison)
        if mic_audio is not None and loopback_data:
            # For dedup energy comparison, use the first loopback's audio
            first_lb_audio = loopback_data[0][0]
            all_segments = self._deduplicate(
                all_segments, mic_audio, first_lb_audio,
            )

        self._report("Complete", 100, f"{len(all_segments)} segments")
        logger.info("Batch transcription complete: %d segments", len(all_segments))

        return all_segments

    # ------------------------------------------------------------------
    # Export helper
    # ------------------------------------------------------------------

    def transcribe_and_export(
        self,
        session_dir: Path,
        stop_event: Optional[Any] = None,
    ) -> Optional[Path]:
        """Re-transcribe and save as a new versioned transcript.

        Convenience method that calls :meth:`transcribe_session` and then
        exports the result via :func:`export_transcript_versioned`.

        Returns
        -------
        Optional[Path]
            Path to the new transcript file, or ``None`` if cancelled or
            no segments were produced.
        """
        segments = self.transcribe_session(session_dir, stop_event)
        if not segments or self._cancelled(stop_event):
            return None

        # Determine session metadata from folder name
        start_dt = self._parse_session_datetime(session_dir)

        # Discover all WAV files for duration calculation
        mic_path, loopback_wavs = self._discover_audio_files(session_dir / "audio")
        duration = 0.0
        all_paths = ([mic_path] if mic_path else []) + [p for p, _ in loopback_wavs]
        for wav_path in all_paths:
            if wav_path.exists():
                info = sf.info(str(wav_path))
                duration = max(duration, info.duration)

        version = next_version_number(session_dir)
        path = export_transcript_versioned(
            segments, session_dir, duration, start_dt, version,
        )
        return path

    # ------------------------------------------------------------------
    # Internal: Audio file discovery
    # ------------------------------------------------------------------

    @staticmethod
    def _discover_audio_files(
        audio_dir: Path,
    ) -> tuple[Optional[Path], list[tuple[Path, str]]]:
        """Discover WAV files in a session's audio directory.

        Reads ``device_manifest.json`` if present.  Falls back to checking
        for ``mic.wav`` and ``system.wav`` directly.

        Returns
        -------
        tuple[Optional[Path], list[tuple[Path, str]]]
            (mic_path, loopback_list) where loopback_list is a list of
            (wav_path, speaker_label) tuples.
        """
        manifest_path = audio_dir / "device_manifest.json"

        if manifest_path.exists():
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                mic_file = data.get("mic", {}).get("file")
                mic_path = (audio_dir / mic_file) if mic_file else None

                loopback_list: list[tuple[Path, str]] = []
                for lb in data.get("loopback", []):
                    lb_file = lb.get("file")
                    if not lb_file:
                        continue
                    lb_path = audio_dir / lb_file
                    label = lb.get("label", "")
                    # Build speaker label: single loopback = "Them", multi = "Them (Label)"
                    n_loopbacks = len(data.get("loopback", []))
                    if n_loopbacks > 1 and label:
                        speaker = f"Them ({label})"
                    else:
                        speaker = "Them"
                    loopback_list.append((lb_path, speaker))

                return mic_path, loopback_list
            except Exception:
                logger.warning("Failed to read device_manifest.json, falling back", exc_info=True)

        # Fallback for old sessions
        mic_path = audio_dir / "mic.wav"
        system_path = audio_dir / "system.wav"

        mic = mic_path if mic_path.exists() else None
        loopbacks: list[tuple[Path, str]] = []
        if system_path.exists():
            loopbacks.append((system_path, "Them"))

        return mic, loopbacks

    # ------------------------------------------------------------------
    # Internal: Whisper transcription
    # ------------------------------------------------------------------

    def _transcribe_channel(
        self,
        audio: np.ndarray,
        speaker: str,
        stop_event: Optional[Any],
    ) -> list[TranscriptSegment]:
        """Run Whisper on a single channel with enhanced settings.

        Audio is RMS-normalized before transcription to ensure consistent
        VAD behavior regardless of channel gain levels.  Mic channels are
        typically much quieter than system channels; without normalization,
        Silero VAD misses quiet mic speech entirely.
        """
        # Normalize audio to target RMS level
        normalized = self._normalize_rms(audio, self._config.target_rms)

        segments_iter, info = self._model.transcribe(
            normalized,
            language="en",
            beam_size=self._config.beam_size,
            vad_filter=True,
            word_timestamps=True,
            condition_on_previous_text=True,
            vad_parameters=dict(
                threshold=self._config.vad_threshold,
                min_silence_duration_ms=self._config.vad_min_silence_ms,
                speech_pad_ms=self._config.vad_speech_pad_ms,
            ),
        )

        result: list[TranscriptSegment] = []
        for segment in segments_iter:
            if self._cancelled(stop_event):
                return result

            text = segment.text.strip()
            if not text:
                continue

            result.append(TranscriptSegment(
                speaker=speaker,
                text=text,
                start_time=segment.start,
                end_time=segment.end,
            ))

        return result

    # ------------------------------------------------------------------
    # Internal: Crosstalk suppression
    # ------------------------------------------------------------------

    def _suppress_crosstalk(
        self,
        segments: list[TranscriptSegment],
        own_audio: np.ndarray,
        other_audio: np.ndarray,
        is_mic: bool,
    ) -> list[TranscriptSegment]:
        """Remove segments that are likely bleed-through from the other channel.

        Compares *normalized* energy between channels to account for gain
        differences (mic is typically much quieter than system audio).
        For each segment, the own-channel and other-channel RMS are each
        divided by their respective full-channel averages.  A segment is
        suppressed only when its normalized own-channel energy is below
        ``energy_threshold`` of the normalized other-channel energy.
        """
        threshold = self._config.energy_threshold

        # Compute average RMS per channel for normalization.
        # This accounts for the typical gain difference between mic and system.
        own_mean_rms = self._rms(own_audio)
        other_mean_rms = self._rms(other_audio)

        if own_mean_rms == 0 or other_mean_rms == 0:
            # Can't normalize — skip suppression for this channel
            logger.info(
                "Crosstalk suppression (%s): skipped — zero energy in channel",
                "mic" if is_mic else "system",
            )
            return segments

        logger.info(
            "Crosstalk suppression (%s): own_mean_rms=%.6f, other_mean_rms=%.6f",
            "mic" if is_mic else "system", own_mean_rms, other_mean_rms,
        )

        kept: list[TranscriptSegment] = []

        for seg in segments:
            start_sample = int(seg.start_time * TARGET_SAMPLERATE)
            end_sample = int(seg.end_time * TARGET_SAMPLERATE)

            # Clamp to array bounds
            start_sample = max(0, start_sample)
            end_own = min(end_sample, len(own_audio))
            end_other = min(end_sample, len(other_audio))

            if end_own <= start_sample:
                kept.append(seg)
                continue

            own_rms = self._rms(own_audio[start_sample:end_own])
            if end_other <= start_sample:
                # Other channel doesn't cover this range — keep it
                kept.append(seg)
                continue

            other_rms = self._rms(other_audio[start_sample:end_other])

            # Normalize each channel's segment energy by its full-channel average.
            # This puts both channels on the same scale regardless of gain.
            norm_own = own_rms / own_mean_rms
            norm_other = other_rms / other_mean_rms if other_mean_rms > 0 else 0.0

            if norm_other > 0 and norm_own / norm_other < threshold:
                logger.info(
                    "Suppressed crosstalk: [%s] %.1f-%.1fs '%.50s' "
                    "(own_rms=%.6f [%.2fx avg], other_rms=%.6f [%.2fx avg], "
                    "norm_ratio=%.3f < %.3f)",
                    seg.speaker, seg.start_time, seg.end_time,
                    seg.text, own_rms, norm_own, other_rms, norm_other,
                    norm_own / norm_other, threshold,
                )
                continue

            kept.append(seg)

        suppressed = len(segments) - len(kept)
        if suppressed:
            logger.info(
                "Crosstalk suppression (%s): removed %d/%d segments",
                "mic" if is_mic else "system", suppressed, len(segments),
            )
        else:
            logger.info(
                "Crosstalk suppression (%s): kept all %d segments",
                "mic" if is_mic else "system", len(segments),
            )
        return kept

    # ------------------------------------------------------------------
    # Internal: Deduplication
    # ------------------------------------------------------------------

    def _deduplicate(
        self,
        segments: list[TranscriptSegment],
        mic_audio: np.ndarray,
        system_audio: np.ndarray,
    ) -> list[TranscriptSegment]:
        """Remove near-duplicate segments from different channels.

        If two segments from different speakers have overlapping times
        and similar text, keep only the one from the louder channel.
        """
        if len(segments) < 2:
            return segments

        to_remove: set[int] = set()
        tolerance = self._config.dedup_time_tolerance
        sim_threshold = self._config.dedup_text_similarity

        for i in range(len(segments)):
            if i in to_remove:
                continue
            for j in range(i + 1, len(segments)):
                if j in to_remove:
                    continue

                a, b = segments[i], segments[j]

                # Only deduplicate across different speakers
                if a.speaker == b.speaker:
                    continue

                # Check time overlap
                if a.start_time > b.end_time + tolerance:
                    break  # sorted by start_time, no more overlaps
                if b.start_time > a.end_time + tolerance:
                    continue

                # Check text similarity
                if not self._texts_similar(a.text, b.text, sim_threshold):
                    continue

                # Keep the louder one
                start_sample = int(min(a.start_time, b.start_time) * TARGET_SAMPLERATE)
                end_sample = int(max(a.end_time, b.end_time) * TARGET_SAMPLERATE)
                start_sample = max(0, start_sample)

                mic_end = min(end_sample, len(mic_audio))
                sys_end = min(end_sample, len(system_audio))

                mic_rms = self._rms(mic_audio[start_sample:mic_end]) if mic_end > start_sample else 0.0
                sys_rms = self._rms(system_audio[start_sample:sys_end]) if sys_end > start_sample else 0.0

                # Remove the weaker one
                if a.speaker == "You":
                    remove_idx = i if mic_rms < sys_rms else j
                else:
                    remove_idx = i if sys_rms < mic_rms else j

                to_remove.add(remove_idx)
                logger.debug(
                    "Dedup: removed segment %d [%s] '%.30s' "
                    "(kept [%s] '%.30s')",
                    remove_idx,
                    segments[remove_idx].speaker,
                    segments[remove_idx].text,
                    segments[i if remove_idx == j else j].speaker,
                    segments[i if remove_idx == j else j].text,
                )

        removed = len(to_remove)
        if removed:
            logger.info("Deduplication: removed %d segments", removed)

        return [s for idx, s in enumerate(segments) if idx not in to_remove]

    # ------------------------------------------------------------------
    # Internal: Utility
    # ------------------------------------------------------------------

    @staticmethod
    def _rms(audio: np.ndarray) -> float:
        """Compute root-mean-square energy of an audio array.

        Returns 0.0 for empty arrays or non-finite results (NaN/inf),
        which can arise from ``np.mean`` on an empty slice.  Callers
        rely on this to guard against division by zero.
        """
        if audio.size == 0:
            return 0.0
        value = float(np.sqrt(np.mean(audio ** 2)))
        if not np.isfinite(value):
            return 0.0
        return value

    @classmethod
    def _normalize_rms(cls, audio: np.ndarray, target_rms: float) -> np.ndarray:
        """Normalize audio to a target RMS level.

        Scales the audio so its RMS equals *target_rms*, then clips to
        [-1.0, 1.0] to prevent clipping.  This ensures Silero VAD works
        consistently regardless of the original recording gain — mic
        channels are typically 5-10x quieter than system channels.

        Returns the original array unchanged if it is empty or silent
        (RMS == 0), so callers don't have to pre-check.
        """
        if audio.size == 0:
            return audio
        current_rms = cls._rms(audio)
        if current_rms <= 0:
            # Silent or non-finite — nothing to normalize.
            return audio
        scale = target_rms / current_rms
        normalized = (audio * scale).astype(np.float32)
        return np.clip(normalized, -1.0, 1.0)

    @staticmethod
    def _texts_similar(a: str, b: str, threshold: float) -> bool:
        """Check if two texts share enough words to be considered duplicates."""
        words_a = set(a.lower().split())
        words_b = set(b.lower().split())
        if not words_a or not words_b:
            return False
        intersection = words_a & words_b
        smaller = min(len(words_a), len(words_b))
        return len(intersection) / smaller >= threshold

    @staticmethod
    def _cancelled(stop_event: Optional[Any]) -> bool:
        """Check if the stop event has been set."""
        return stop_event is not None and stop_event.is_set()

    @staticmethod
    def _parse_session_datetime(session_dir: Path) -> datetime:
        """Parse session start datetime from the folder name."""
        m = _SESSION_DIR_RE.match(session_dir.name)
        if m:
            return datetime.strptime(m.group(1), "%Y-%m-%d_%H%M%S")
        # Fallback to folder modification time
        return datetime.fromtimestamp(session_dir.stat().st_mtime)

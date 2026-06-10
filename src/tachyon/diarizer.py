"""Speaker diarization engine for post-processing session audio.

Supports two source modes:

- **system**: splits non-"You" / loopback segments using system WAV files.
- **mixed**: splits all transcript segments using a single mixed audio file
  (typically ``mic.wav``) when multiple speakers share one recording.

**auto** picks system when enough non-"You" segments exist, otherwise mixed.

Supports three switchable embedding backends:
  - **speechbrain** (default): ECAPA-TDNN 192-dim embeddings, 0.80% EER on VoxCeleb
  - **pyannote** (optional): ~512-dim embeddings, requires HF token + model acceptance
  - **resemblyzer** (lightweight fallback): 256-dim embeddings, older GE2E model

All three run on CPU and share the same clustering pipeline.

Pipeline:
  1. Load & preprocess system.wav (resample to 16kHz, RMS-normalize)
  2. Load source transcript segments + estimate end_times
  3. Extract sliding window embeddings across full audio (3s windows, 1.5s hop)
  4. Agglomerative clustering with max-silhouette auto speaker count
  5. Build per-second speaker timeline from window labels (majority vote)
  6. Assign transcript segments from timeline (majority vote per segment span)
  7. Merge consecutive same-speaker segments
  8. Build per-speaker profiles

The sliding window approach decouples embedding extraction from transcript
segment boundaries, which avoids the noise caused by Whisper splitting text
at arbitrary points that don't align with speaker turns.

Threading
---------
The ``diarize_session`` method runs on a worker thread (same slot as batch).
Progress updates arrive via callback -- caller must use ``root.after()``
for tkinter updates.
"""

from __future__ import annotations

import json
import logging
import math
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import numpy as np
import soundfile as sf
import soxr

from tachyon.config import PROJECT_ROOT
from tachyon.session import TranscriptSegment
from tachyon.exporter import load_transcript_from_markdown
from tachyon.model_pins import (
    HF_TOKEN_SETTINGS_URL,
    PYANNOTE_EMBEDDING_REPO,
    PYANNOTE_EMBEDDING_REVISION,
    PYANNOTE_EMBEDDING_URL,
    SPEECHBRAIN_ECAPA_REPO,
    SPEECHBRAIN_ECAPA_REVISION,
)

logger = logging.getLogger(__name__)


class PyannoteAccessError(RuntimeError):
    """Raised when pyannote model load fails due to HF token or model terms."""

# Absolute path for speechbrain model cache (survives cwd changes)
_SPEECHBRAIN_SAVEDIR: str = str(PROJECT_ROOT / "models" / "speechbrain-ecapa")

TARGET_SAMPLERATE: int = 16_000

# Speaker color palette -- index 0 is reserved for "You"
SPEAKER_COLORS: list[str] = [
    "#66b3ff",  # Blue   -- reserved for "You"
    "#ff9966",  # Orange -- Speaker 1
    "#77dd77",  # Green  -- Speaker 2
    "#ff6b6b",  # Red    -- Speaker 3
    "#c49bff",  # Purple -- Speaker 4
    "#ffd700",  # Gold   -- Speaker 5
    "#ff69b4",  # Pink   -- Speaker 6
    "#40e0d0",  # Teal   -- Speaker 7
]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class DiarizeConfig:
    """Configuration for speaker diarization.

    Attributes:
        min_speakers: Minimum speaker count for auto-detection (default 2).
        max_speakers: Maximum speaker count for auto-detection (default 8).
        num_speakers: Exact speaker count override (bypasses auto-detection).
        backend: Embedding backend — "speechbrain", "pyannote", or "resemblyzer".
        hf_token: HuggingFace token, required for pyannote backend only.
        audio_mode: Audio source — "auto", "system", or "mixed".
        audio_file: Optional WAV filename override within ``audio/`` for mixed mode.
    """

    min_speakers: Optional[int] = None
    max_speakers: Optional[int] = None
    num_speakers: Optional[int] = None
    backend: str = "speechbrain"
    hf_token: str = ""
    audio_mode: str = "auto"
    audio_file: Optional[str] = None


@dataclass
class DiarizeProgress:
    """Progress update from the diarization process.

    Attributes:
        phase: Human-readable phase name.
        percent: Overall completion percentage (0-100).
        detail: Optional detail string.
    """

    phase: str
    percent: int
    detail: str = ""


@dataclass
class SpeakerInfo:
    """Profile information for a detected speaker.

    Attributes:
        id: Internal speaker ID ("speaker_1", "speaker_2", ...).
        display_name: Display name ("Speaker 1" or user-assigned name).
        total_duration: Total speaking time in seconds.
        segment_count: Number of transcript segments assigned.
        sample_texts: First 3 segment texts (truncated to 80 chars).
        color_index: Index into SPEAKER_COLORS palette.
    """

    id: str
    display_name: str
    total_duration: float
    segment_count: int
    sample_texts: list[str]
    color_index: int


# ---------------------------------------------------------------------------
# Diarizer engine
# ---------------------------------------------------------------------------

class Diarizer:
    """Speaker diarization engine using neural speaker embeddings + clustering.

    Parameters
    ----------
    config:
        Diarization settings.  Uses defaults if not provided.
    on_progress:
        Optional callback invoked with :class:`DiarizeProgress` updates.
        Called from the worker thread.
    """

    def __init__(
        self,
        config: Optional[DiarizeConfig] = None,
        on_progress: Optional[Callable[[DiarizeProgress], None]] = None,
    ) -> None:
        self._config = config or DiarizeConfig()
        self._on_progress = on_progress
        self._encoder: Any = None

    def _report(self, phase: str, percent: int, detail: str = "") -> None:
        """Emit a progress update."""
        if self._on_progress is not None:
            self._on_progress(DiarizeProgress(phase=phase, percent=percent, detail=detail))

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def diarize_session(
        self,
        session_dir: Path,
        source_transcript: str,
        stop_event: Optional[Any] = None,
    ) -> Optional[tuple[list[TranscriptSegment], list[SpeakerInfo]]]:
        """Run speaker diarization on a session's audio and transcript.

        Resolves audio from loopback WAVs (system mode) or ``mic.wav``
        (mixed mode) based on :attr:`DiarizeConfig.audio_mode`.

        Parameters
        ----------
        session_dir:
            Path to the session folder containing audio files and
            the source transcript file.
        source_transcript:
            Filename of the transcript to relabel (e.g. "transcript.md").
        stop_event:
            Optional ``threading.Event`` for cancellation.

        Returns
        -------
        Optional[tuple[list[TranscriptSegment], list[SpeakerInfo]]]
            Tuple of (relabeled_segments, speaker_info_list), or None
            if cancelled or no suitable audio found.
        """
        transcript_path = session_dir / source_transcript
        if not transcript_path.exists():
            logger.error("Source transcript not found: %s", transcript_path)
            return None

        audio_dir = session_dir / "audio"

        # Step 1: Load source transcript segments
        self._report("Loading transcript", 5)
        _, source_segments = load_transcript_from_markdown(transcript_path)
        if not source_segments:
            logger.error("Source transcript has no segments: %s", transcript_path)
            return None

        resolved = self._resolve_audio_sources(audio_dir, source_segments)
        if resolved is None:
            logger.error("No suitable audio found for diarization in %s", session_dir)
            return None

        audio_sources, effective_mode = resolved
        preserve_you = effective_mode == "system"
        logger.info(
            "Diarization audio mode: requested=%s, effective=%s, files=%d",
            self._config.audio_mode, effective_mode, len(audio_sources),
        )

        # Step 2: Load audio from resolved WAV file(s)
        self._report("Loading audio", 10)
        audio_parts: list[np.ndarray] = []
        audio_duration_sec = 0.0
        for wav_path, _label in audio_sources:
            part = self._load_audio(wav_path)
            if part is not None and part.size > 0:
                audio_parts.append(part)
                audio_duration_sec = max(audio_duration_sec, len(part) / TARGET_SAMPLERATE)

        if not audio_parts:
            logger.error("Failed to load any diarization audio")
            return None

        if self._cancelled(stop_event):
            return None

        # Estimate end_time as the next segment's start_time (or audio end).
        source_segments = self._estimate_end_times(source_segments, audio_duration_sec)

        them_segments = [s for s in source_segments if s.speaker != "You"]
        logger.info(
            "Loaded %d segments (%d non-You), mode=%s",
            len(source_segments), len(them_segments), effective_mode,
        )

        if effective_mode == "system":
            if len(them_segments) < 2:
                logger.warning(
                    "Not enough 'Them' segments for clustering (%d)", len(them_segments),
                )
                return None
        elif len(source_segments) < 2:
            logger.warning(
                "Not enough transcript segments for mixed diarization (%d)",
                len(source_segments),
            )
            return None

        if self._cancelled(stop_event):
            return None

        # Step 3: Extract sliding window embeddings (load encoder first to fail fast)
        self._report("Loading speaker encoder", 20)
        try:
            self._get_encoder()
        except PyannoteAccessError:
            raise
        except Exception as exc:
            logger.error("Failed to load %s encoder: %s", self._config.backend, exc)
            self._report("Error", 0, f"Encoder load failed: {exc}")
            return None

        self._report("Extracting speaker embeddings", 25)
        feature_batches: list[np.ndarray] = []
        window_centers: list[float] = []

        for audio in audio_parts:
            part_features, part_centers = self._extract_window_features(audio)
            if len(part_features) > 0:
                feature_batches.append(part_features)
                window_centers.extend(part_centers)

        if feature_batches:
            features = np.concatenate(feature_batches, axis=0)
        else:
            features = np.array([])

        logger.info(
            "Extracted %d window embeddings (%d dims) from %d audio file(s), %.1fs max duration",
            len(features),
            features.shape[1] if len(features) > 0 else 0,
            len(audio_parts),
            audio_duration_sec,
        )

        if len(features) < 2:
            logger.warning("Not enough valid window embeddings for clustering (%d)", len(features))
            return None

        if self._cancelled(stop_event):
            return None

        # Step 4: Normalize features
        self._report("Normalizing features", 45)
        features_scaled = self._normalize_features(features)

        if self._cancelled(stop_event):
            return None

        # Step 5: Cluster speakers + build per-second timeline
        self._report("Clustering speakers", 55)
        labels = self._cluster_speakers(features_scaled)
        num_speakers = len(set(labels))
        logger.info("Detected %d speakers", num_speakers)

        self._report("Building speaker timeline", 65)
        timeline_resolution_sec = 0.25
        timeline = self._build_speaker_timeline(
            window_centers,
            labels,
            window_sec=3.0,
            audio_duration=audio_duration_sec,
            resolution_sec=timeline_resolution_sec,
        )
        logger.info(
            "Built timeline with %d bins at %.2fs resolution",
            len(timeline), timeline_resolution_sec,
        )

        if self._cancelled(stop_event):
            return None

        # Step 6: Relabel segments from timeline (majority vote)
        self._report("Relabeling segments", 75)
        relabeled = self._relabel_from_timeline(
            source_segments,
            timeline,
            labels,
            resolution_sec=timeline_resolution_sec,
            preserve_you=preserve_you,
        )

        if self._cancelled(stop_event):
            return None

        # Step 7: Merge consecutive same-speaker segments
        self._report("Merging segments", 85)
        merged = self._merge_consecutive_segments(relabeled)
        logger.info(
            "Merged %d segments → %d segments",
            len(relabeled), len(merged),
        )

        if self._cancelled(stop_event):
            return None

        # Step 8: Build speaker profiles
        self._report("Building speaker profiles", 90)
        speaker_info = self._build_speaker_info(merged)

        self._report("Complete", 100, f"{num_speakers} speakers detected")
        return merged, speaker_info

    # ------------------------------------------------------------------
    # Audio file discovery
    # ------------------------------------------------------------------

    @staticmethod
    def _discover_loopback_wavs(audio_dir: Path) -> list[tuple[Path, str]]:
        """Discover loopback WAV files from device_manifest.json or fallback.

        Returns a list of (wav_path, label) tuples for existing files.
        """
        manifest_path = audio_dir / "device_manifest.json"

        if manifest_path.exists():
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                result: list[tuple[Path, str]] = []
                for lb in data.get("loopback", []):
                    lb_file = lb.get("file")
                    if lb_file:
                        p = audio_dir / lb_file
                        if p.exists():
                            result.append((p, lb.get("label", "")))
                if result:
                    return result
            except Exception:
                pass

        # Fallback: check for system.wav
        system_wav = audio_dir / "system.wav"
        if system_wav.exists():
            return [(system_wav, "")]

        return []

    @staticmethod
    def _discover_mic_wav(
        audio_dir: Path,
        audio_file: Optional[str] = None,
    ) -> Optional[Path]:
        """Discover the mic / mixed WAV file from manifest or fallback."""
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

    def _resolve_audio_sources(
        self,
        audio_dir: Path,
        source_segments: list[TranscriptSegment],
    ) -> Optional[tuple[list[tuple[Path, str]], str]]:
        """Resolve WAV files and effective mode from config and transcript.

        Returns
        -------
        Optional[tuple[list[tuple[Path, str]], str]]
            ``(wav_list, effective_mode)`` where *effective_mode* is
            ``"system"`` or ``"mixed"``, or ``None`` if no audio found.
        """
        mode = (self._config.audio_mode or "auto").strip().lower()
        them_segments = [s for s in source_segments if s.speaker != "You"]
        loopbacks = self._discover_loopback_wavs(audio_dir)
        mic_path = self._discover_mic_wav(audio_dir, self._config.audio_file)

        if mode == "system":
            if not loopbacks:
                return None
            return loopbacks, "system"

        if mode == "mixed":
            if mic_path is None:
                return None
            return [(mic_path, "mic")], "mixed"

        if mode == "auto":
            if loopbacks and len(them_segments) >= 2:
                return loopbacks, "system"
            if mic_path is not None and len(source_segments) >= 2:
                return [(mic_path, "mic")], "mixed"
            if loopbacks:
                return loopbacks, "system"
            if mic_path is not None:
                return [(mic_path, "mic")], "mixed"
            return None

        logger.error("Unknown audio_mode: %s", self._config.audio_mode)
        return None

    # ------------------------------------------------------------------
    # Step 1: Load & preprocess audio
    # ------------------------------------------------------------------

    def _load_audio(self, path: Path) -> Optional[np.ndarray]:
        """Load a WAV file, resample to 16kHz mono, RMS-normalize."""
        try:
            audio, sr = sf.read(str(path), dtype="float32")
        except Exception:
            logger.exception("Failed to read %s", path)
            return None

        # Convert to mono if stereo
        if audio.ndim > 1:
            audio = audio.mean(axis=1).astype(np.float32)

        # Resample to 16kHz
        if sr != TARGET_SAMPLERATE:
            audio = soxr.resample(audio, sr, TARGET_SAMPLERATE).astype(np.float32)

        # RMS-normalize to consistent level
        rms = float(np.sqrt(np.mean(audio ** 2)))
        if rms > 0:
            target_rms = 0.05
            scale = target_rms / rms
            audio = np.clip(audio * scale, -1.0, 1.0).astype(np.float32)

        logger.info(
            "Loaded diarization audio (%s): %.1fs, %d samples",
            path.name,
            len(audio) / TARGET_SAMPLERATE, len(audio),
        )
        return audio

    # ------------------------------------------------------------------
    # Step 2b: Estimate missing end_times
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_end_times(
        segments: list[TranscriptSegment],
        audio_duration: float,
    ) -> list[TranscriptSegment]:
        """Fix segments where end_time == start_time (parsed from markdown).

        Sets each segment's end_time to the next segment's start_time,
        or to the audio duration for the last segment.  Caps at 15s max
        per segment to avoid one segment swallowing a long silence.
        """
        max_segment_duration = 15.0
        fixed: list[TranscriptSegment] = []

        for i, seg in enumerate(segments):
            if seg.end_time > seg.start_time:
                # Already has a valid end_time
                fixed.append(seg)
                continue

            # Estimate end_time from next segment's start_time
            if i + 1 < len(segments):
                next_start = segments[i + 1].start_time
            else:
                next_start = audio_duration

            estimated_end = min(next_start, seg.start_time + max_segment_duration)

            fixed.append(TranscriptSegment(
                speaker=seg.speaker,
                text=seg.text,
                start_time=seg.start_time,
                end_time=estimated_end,
            ))

        return fixed

    # ------------------------------------------------------------------
    # Step 3: Sliding window feature extraction
    # ------------------------------------------------------------------

    def _extract_window_features(
        self,
        audio: np.ndarray,
        window_sec: float = 3.0,
        hop_sec: float = 1.5,
        energy_thresh: float = 0.01,
    ) -> tuple[np.ndarray, list[float]]:
        """Extract speaker embeddings from fixed-size sliding windows.

        Sweeps overlapping windows across the full audio, independently
        of transcript segment boundaries.  Skips near-silent windows.

        Parameters
        ----------
        audio:
            Mono float32 audio at TARGET_SAMPLERATE.
        window_sec:
            Window duration in seconds.
        hop_sec:
            Hop between window starts in seconds.
        energy_thresh:
            Minimum RMS energy to keep a window (skip silence).

        Returns
        -------
        tuple[np.ndarray, list[float]]
            (features_matrix, window_centers_sec) where features_matrix
            has shape (n_valid, embed_dim) and window_centers_sec is the
            center time of each kept window.
        """
        window_samples = int(window_sec * TARGET_SAMPLERATE)
        hop_samples = int(hop_sec * TARGET_SAMPLERATE)
        total_samples = len(audio)

        feature_list: list[np.ndarray] = []
        centers: list[float] = []

        pos = 0
        while pos + window_samples <= total_samples:
            window_audio = audio[pos : pos + window_samples]

            # Skip near-silent windows
            rms = float(np.sqrt(np.mean(window_audio ** 2)))
            if rms < energy_thresh:
                pos += hop_samples
                continue

            feat = self._compute_segment_features(window_audio)
            if feat is not None:
                feature_list.append(feat)
                center = (pos + window_samples / 2) / TARGET_SAMPLERATE
                centers.append(center)

            pos += hop_samples

        if not feature_list:
            return np.array([]), []

        return np.array(feature_list, dtype=np.float32), centers

    def _get_encoder(self) -> Any:
        """Lazy-load the speaker embedding encoder for the configured backend."""
        if self._encoder is None:
            backend = self._config.backend
            if backend == "speechbrain":
                # Patch torchaudio compatibility: speechbrain 1.0.x calls
                # torchaudio.list_audio_backends() which was removed in
                # torchaudio >=2.9.  Provide a shim so it doesn't crash.
                import torchaudio
                if not hasattr(torchaudio, "list_audio_backends"):
                    torchaudio.list_audio_backends = lambda: ["soundfile"]
                from speechbrain.inference.speaker import EncoderClassifier
                self._encoder = _load_speechbrain_pinned(
                    EncoderClassifier, _SPEECHBRAIN_SAVEDIR,
                )
            elif backend == "pyannote":
                from pyannote.audio import Model, Inference
                if not self._config.hf_token:
                    raise RuntimeError(
                        "pyannote backend requires a HuggingFace token. "
                        "Set hf_token in config.json or enter it when prompted."
                    )
                model = _load_pyannote_pinned(Model, self._config.hf_token)
                self._encoder = Inference(model, window="whole")
            elif backend == "resemblyzer":
                from resemblyzer import VoiceEncoder
                self._encoder = VoiceEncoder()
            else:
                raise ValueError(f"Unknown diarization backend: {backend}")
            logger.info("Loaded %s speaker encoder", backend)
        return self._encoder

    def _compute_segment_features(
        self, segment_audio: np.ndarray,
    ) -> Optional[np.ndarray]:
        """Compute a speaker embedding for one audio segment.

        Dispatches to the configured backend (speechbrain, pyannote,
        or resemblyzer) to produce a fixed-dimension embedding vector.
        """
        try:
            backend = self._config.backend
            encoder = self._get_encoder()

            if backend == "speechbrain":
                return self._embed_speechbrain(encoder, segment_audio)
            elif backend == "pyannote":
                return self._embed_pyannote(encoder, segment_audio)
            elif backend == "resemblyzer":
                return self._embed_resemblyzer(encoder, segment_audio)
        except Exception:
            logger.warning("Feature extraction failed for segment", exc_info=True)
            return None

    def _embed_speechbrain(
        self, encoder: Any, audio: np.ndarray,
    ) -> Optional[np.ndarray]:
        """Compute a 192-dim ECAPA-TDNN embedding via speechbrain."""
        import torch
        tensor = torch.from_numpy(audio).unsqueeze(0)  # (1, time)
        embedding = encoder.encode_batch(tensor)         # (1, 1, 192)
        return embedding.squeeze().cpu().numpy()          # (192,)

    def _embed_pyannote(
        self, inference: Any, audio: np.ndarray,
    ) -> Optional[np.ndarray]:
        """Compute a ~512-dim embedding via pyannote."""
        import torch
        waveform = torch.from_numpy(audio).unsqueeze(0).float()  # (1, time)
        embedding = inference({"waveform": waveform, "sample_rate": TARGET_SAMPLERATE})
        return embedding.squeeze()  # pyannote returns numpy already

    def _embed_resemblyzer(
        self, encoder: Any, audio: np.ndarray,
    ) -> Optional[np.ndarray]:
        """Compute a 256-dim GE2E embedding via resemblyzer."""
        from resemblyzer import preprocess_wav
        processed = preprocess_wav(audio, source_sr=TARGET_SAMPLERATE)
        if len(processed) < 1600:  # < 0.1s after preprocessing
            return None
        return encoder.embed_utterance(processed)  # (256,)

    # ------------------------------------------------------------------
    # Step 4: Feature normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_features(features: np.ndarray) -> np.ndarray:
        """Pass through — all three backends produce well-scaled embeddings.

        speechbrain, pyannote, and resemblyzer all produce L2-normalized or
        well-scaled embeddings. StandardScaler would distort the embedding
        space. For unit vectors, euclidean distance is monotonically related
        to cosine distance, so ward-linkage clustering works directly.
        """
        return features

    # ------------------------------------------------------------------
    # Step 5: Clustering with max-silhouette selection
    # ------------------------------------------------------------------

    def _cluster_speakers(self, features: np.ndarray) -> np.ndarray:
        """Cluster feature vectors to identify distinct speakers.

        If num_speakers is set, uses that directly.  Otherwise tries
        k=2..8 and picks the k with the highest silhouette score.
        When all scores are weak (< 0.25), defaults to k=2 to avoid
        over-splitting.
        """
        from sklearn.cluster import AgglomerativeClustering
        from sklearn.metrics import silhouette_score

        n_samples = features.shape[0]

        if self._config.num_speakers is not None:
            n = min(self._config.num_speakers, n_samples)
            model = AgglomerativeClustering(n_clusters=n, linkage="ward")
            return model.fit_predict(features)

        min_k = self._config.min_speakers or 2
        max_k = self._config.max_speakers or 8
        max_k = min(max_k, n_samples)

        if min_k >= max_k:
            model = AgglomerativeClustering(n_clusters=min_k, linkage="ward")
            return model.fit_predict(features)

        # Collect silhouette scores for each k
        k_values: list[int] = []
        scores: list[float] = []
        all_labels: dict[int, np.ndarray] = {}

        for k in range(min_k, max_k + 1):
            if k >= n_samples:
                break

            model = AgglomerativeClustering(n_clusters=k, linkage="ward")
            labels = model.fit_predict(features)

            unique_labels = set(labels)
            if len(unique_labels) < 2:
                continue

            try:
                score = silhouette_score(features, labels)
            except ValueError:
                continue

            logger.info("Clustering k=%d: silhouette=%.3f", k, score)

            k_values.append(k)
            scores.append(score)
            all_labels[k] = labels

        if not k_values:
            model = AgglomerativeClustering(n_clusters=2, linkage="ward")
            return model.fit_predict(features)

        # Pick the k with the highest silhouette score
        best_idx = scores.index(max(scores))
        best_k = k_values[best_idx]
        best_score = scores[best_idx]

        logger.info("Selected k=%d (silhouette=%.3f)", best_k, best_score)
        return all_labels[best_k]

    # ------------------------------------------------------------------
    # Step 5b: Build per-second speaker timeline from window labels
    # ------------------------------------------------------------------

    @staticmethod
    def _build_speaker_timeline(
        window_centers: list[float],
        labels: np.ndarray,
        window_sec: float,
        audio_duration: float,
        resolution_sec: float = 0.25,
    ) -> dict[int, int]:
        """Build a fixed-resolution speaker timeline via majority vote.

        For each timeline bin, counts votes from all windows whose
        span covers that bin timestamp, then assigns the speaker label with the
        most votes.

        Parameters
        ----------
        window_centers:
            Center time (seconds) of each kept window.
        labels:
            Cluster label for each window (same length as window_centers).
        window_sec:
            Window duration in seconds (used to compute span).
        audio_duration:
            Total audio duration in seconds.
        resolution_sec:
            Timeline bin size in seconds.

        Returns
        -------
        dict[int, int]
            Mapping of bin index → speaker cluster label.
        """
        half_win = window_sec / 2.0
        num_seconds = int(math.ceil(audio_duration / resolution_sec)) + 1
        timeline: dict[int, int] = {}

        for sec in range(num_seconds):
            timestamp = sec * resolution_sec
            votes: dict[int, int] = {}
            for i, center in enumerate(window_centers):
                win_start = center - half_win
                win_end = center + half_win
                # Window covers this bin if the bin timestamp falls within [win_start, win_end)
                if win_start <= timestamp < win_end:
                    lbl = int(labels[i])
                    votes[lbl] = votes.get(lbl, 0) + 1

            if votes:
                timeline[sec] = max(votes, key=votes.get)  # type: ignore[arg-type]

        return timeline

    # ------------------------------------------------------------------
    # Step 6: Segment relabeling from timeline
    # ------------------------------------------------------------------

    def _relabel_from_timeline(
        self,
        all_segments: list[TranscriptSegment],
        timeline: dict[int, int],
        labels: np.ndarray,
        resolution_sec: float = 0.25,
        preserve_you: bool = True,
    ) -> list[TranscriptSegment]:
        """Assign speaker labels to segments using the per-second timeline.

        For each segment, looks up its time range in the timeline and
        assigns the speaker with the most seconds covered.  When
        *preserve_you* is True (system mode), ``"You"`` segments pass
        through unchanged.  When False (mixed mode), all segments are
        relabeled.

        Parameters
        ----------
        all_segments:
            All transcript segments (both "You" and non-"You").
        timeline:
            Per-second speaker mapping from _build_speaker_timeline().
        labels:
            Raw cluster labels (used to build cluster → speaker number mapping).
        preserve_you:
            If True, keep ``"You"`` segments unchanged.
        """
        # Build mapping: cluster label → speaker number (by first appearance in labels)
        first_appearance: dict[int, int] = {}
        for i, label in enumerate(labels):
            label_int = int(label)
            if label_int not in first_appearance:
                first_appearance[label_int] = i

        sorted_clusters = sorted(first_appearance.items(), key=lambda x: x[1])
        cluster_to_speaker: dict[int, int] = {}
        for idx, (cluster_label, _) in enumerate(sorted_clusters):
            cluster_to_speaker[cluster_label] = idx + 1  # 1-based

        # Relabel segments
        relabeled: list[TranscriptSegment] = []
        for seg in all_segments:
            if preserve_you and seg.speaker == "You":
                relabeled.append(seg)
                continue

            # Count speaker votes from timeline bins covering this segment
            start_sec = int(seg.start_time / resolution_sec)
            end_sec = int(math.ceil(seg.end_time / resolution_sec))
            if end_sec <= start_sec:
                end_sec = start_sec + 1  # minimum one-bin span

            votes: dict[int, int] = {}
            for sec in range(start_sec, end_sec + 1):
                cluster_label = timeline.get(sec)
                if cluster_label is not None:
                    speaker_num = cluster_to_speaker.get(cluster_label, 1)
                    votes[speaker_num] = votes.get(speaker_num, 0) + 1

            if votes:
                speaker_num = max(votes, key=votes.get)  # type: ignore[arg-type]
            else:
                speaker_num = 1  # fallback if no timeline coverage

            relabeled.append(TranscriptSegment(
                speaker=f"Speaker {speaker_num}",
                text=seg.text,
                start_time=seg.start_time,
                end_time=seg.end_time,
            ))

        return relabeled

    # ------------------------------------------------------------------
    # Step 7: Merge consecutive same-speaker segments
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_consecutive_segments(
        segments: list[TranscriptSegment],
    ) -> list[TranscriptSegment]:
        """Merge consecutive segments from the same speaker.

        When diarization assigns the same label to adjacent segments,
        they should be combined into a single block with the first
        segment's start_time, the last segment's end_time, and all
        text joined with a space.
        """
        if not segments:
            return []

        merged: list[TranscriptSegment] = []
        current = segments[0]

        for seg in segments[1:]:
            if seg.speaker == current.speaker:
                # Same speaker — merge text and extend end_time
                current = TranscriptSegment(
                    speaker=current.speaker,
                    text=current.text + " " + seg.text,
                    start_time=current.start_time,
                    end_time=seg.end_time,
                )
            else:
                # Different speaker — flush current, start new
                merged.append(current)
                current = seg

        merged.append(current)
        return merged

    # ------------------------------------------------------------------
    # Step 8: Speaker profiling
    # ------------------------------------------------------------------

    def _build_speaker_info(
        self,
        segments: list[TranscriptSegment],
    ) -> list[SpeakerInfo]:
        """Build per-speaker profile information."""
        speaker_stats: dict[str, dict] = {}

        for seg in segments:
            if seg.speaker == "You":
                continue

            name = seg.speaker
            if name not in speaker_stats:
                speaker_stats[name] = {
                    "duration": 0.0,
                    "count": 0,
                    "texts": [],
                }

            stats = speaker_stats[name]
            duration = seg.end_time - seg.start_time
            if duration <= 0:
                duration = 1.0  # minimum 1s for segments without end_time
            stats["duration"] += duration
            stats["count"] += 1
            if len(stats["texts"]) < 3:
                text = seg.text[:80] + "..." if len(seg.text) > 80 else seg.text
                stats["texts"].append(text)

        # Build SpeakerInfo list sorted by speaker number
        infos: list[SpeakerInfo] = []
        for name in sorted(speaker_stats.keys(), key=_speaker_sort_key):
            stats = speaker_stats[name]
            num = _parse_speaker_number(name)
            color_idx = min(num, len(SPEAKER_COLORS) - 1)

            infos.append(SpeakerInfo(
                id=f"speaker_{num}",
                display_name=name,
                total_duration=stats["duration"],
                segment_count=stats["count"],
                sample_texts=stats["texts"],
                color_index=color_idx,
            ))

        return infos

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def _cancelled(stop_event: Optional[Any]) -> bool:
        """Check if the stop event has been set."""
        return stop_event is not None and stop_event.is_set()


# ---------------------------------------------------------------------------
# Speaker map persistence
# ---------------------------------------------------------------------------

def save_speaker_map(
    session_dir: Path,
    speaker_map: dict[str, str],
    num_speakers: int,
    source_transcript: str,
) -> Path:
    """Save the speaker name mapping to speaker_map.json.

    Parameters
    ----------
    session_dir:
        Session folder path.
    speaker_map:
        Mapping of speaker_id ("speaker_1") to display name ("John").
    num_speakers:
        Number of speakers detected.
    source_transcript:
        Filename of the transcript used as source.

    Returns
    -------
    Path
        Path to the saved JSON file.
    """
    data = {
        "speaker_map": speaker_map,
        "num_speakers_detected": num_speakers,
        "source_transcript": source_transcript,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }

    path = session_dir / "speaker_map.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("Speaker map saved to %s", path)
    return path


def load_speaker_map(session_dir: Path) -> Optional[dict]:
    """Load the speaker map from speaker_map.json.

    Returns
    -------
    Optional[dict]
        The full JSON dict, or None if the file does not exist.
    """
    path = session_dir / "speaker_map.json"
    if not path.exists():
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Failed to load speaker_map.json", exc_info=True)
        return None


def update_speaker_names(
    session_dir: Path,
    name_updates: dict[str, str],
) -> None:
    """Update speaker names in an existing speaker_map.json.

    Parameters
    ----------
    session_dir:
        Session folder path.
    name_updates:
        Mapping of speaker_id to new display name.
    """
    data = load_speaker_map(session_dir)
    if data is None:
        logger.warning("No speaker_map.json to update in %s", session_dir)
        return

    for speaker_id, name in name_updates.items():
        data["speaker_map"][speaker_id] = name

    path = session_dir / "speaker_map.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("Speaker names updated in %s", path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _speaker_sort_key(name: str) -> int:
    """Sort key for speaker names: 'Speaker 1' -> 1."""
    return _parse_speaker_number(name)


def _parse_speaker_number(name: str) -> int:
    """Extract the speaker number from a name like 'Speaker 2'."""
    try:
        return int(name.split()[-1])
    except (ValueError, IndexError):
        return 0


# ---------------------------------------------------------------------------
# Pinned model loaders
# ---------------------------------------------------------------------------

def _load_speechbrain_pinned(encoder_cls: Any, savedir: str) -> Any:
    """Load the speechbrain ECAPA encoder pinned to a known commit.

    Falls back to an unpinned load with a loud warning if the installed
    speechbrain build does not accept ``revision`` -- prefer upgrading
    speechbrain rather than running unpinned.
    """
    common_kwargs: dict[str, Any] = dict(
        source=SPEECHBRAIN_ECAPA_REPO,
        savedir=savedir,
        run_opts={"device": "cpu"},
    )
    try:
        return encoder_cls.from_hparams(
            revision=SPEECHBRAIN_ECAPA_REVISION, **common_kwargs,
        )
    except TypeError:
        logger.warning(
            "speechbrain.from_hparams does not accept 'revision' -- "
            "loading %s UNPINNED.  Upgrade speechbrain to restore "
            "supply-chain pinning.",
            SPEECHBRAIN_ECAPA_REPO,
        )
        return encoder_cls.from_hparams(**common_kwargs)


def _pyannote_access_message() -> str:
    """Return user-facing guidance for pyannote HF token/model-terms failures."""
    return (
        "Pyannote model access failed. Accept model terms at "
        f"{PYANNOTE_EMBEDDING_URL}, verify your HuggingFace token at "
        f"{HF_TOKEN_SETTINGS_URL}, or switch to SpeechBrain."
    )


def _is_pyannote_access_error(exc: Exception) -> bool:
    """Return True when an exception likely indicates HF access/terms issues."""
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


@contextmanager
def _torch_load_legacy_pickle_for_pinned_pyannote() -> Iterator[None]:
    """Temporarily allow legacy pickle checkpoint loading for pinned pyannote.

    PyTorch 2.6 changed ``torch.load`` to default to ``weights_only=True``.
    The pinned pyannote embedding checkpoint is an older Lightning checkpoint
    that stores callback/OmegaConf metadata and cannot load in weights-only
    mode. This is intentionally scoped to the pinned pyannote load call because
    legacy pickle loading is a code-execution boundary.
    """
    import torch

    original_load = torch.load

    def _load_with_legacy_pickle(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("weights_only") is None:
            kwargs["weights_only"] = False
        return original_load(*args, **kwargs)

    torch.load = _load_with_legacy_pickle
    try:
        yield
    finally:
        torch.load = original_load


def _load_pyannote_pinned(model_cls: Any, hf_token: str) -> Any:
    """Load the pyannote embedding model pinned to a known commit.

    pyannote.audio 3.x expects Hugging Face auth as ``use_auth_token``
    and reads revisions from the ``repo@revision`` checkpoint string.
    Passing ``token=...`` is silently ignored because the loader accepts
    extra model-constructor kwargs.
    """
    checkpoint = f"{PYANNOTE_EMBEDDING_REPO}@{PYANNOTE_EMBEDDING_REVISION}"
    try:
        with _torch_load_legacy_pickle_for_pinned_pyannote():
            model = model_cls.from_pretrained(
                checkpoint,
                use_auth_token=hf_token,
            )
    except TypeError:
        logger.warning(
            "pyannote.Model.from_pretrained does not accept 'use_auth_token' -- "
            "retrying %s with token=... for compatibility.",
            PYANNOTE_EMBEDDING_REPO,
        )
        try:
            with _torch_load_legacy_pickle_for_pinned_pyannote():
                model = model_cls.from_pretrained(
                    checkpoint,
                    token=hf_token,
                )
        except Exception as exc:
            if _is_pyannote_access_error(exc):
                raise PyannoteAccessError(_pyannote_access_message()) from exc
            raise
    except Exception as exc:
        if _is_pyannote_access_error(exc):
            raise PyannoteAccessError(_pyannote_access_message()) from exc
        raise

    if model is None:
        raise PyannoteAccessError(_pyannote_access_message())

    return model

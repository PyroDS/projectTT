"""Real-time transcription engine using faster-whisper with CUDA.

Consumes AudioChunk objects from a shared queue, runs them through a
GPU-accelerated Whisper model, and emits TranscriptSegment objects via a
callback. Uses a rolling overlap buffer per audio source to eliminate
duplicate or split words at chunk boundaries.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable, Dict, Optional

import numpy as np

from tachyon.capture import AudioChunk
from tachyon.session import TranscriptSegment

logger = logging.getLogger(__name__)

# Number of samples of overlap to prepend from the previous chunk.
# At 16 kHz this is ~1 second of audio context for the model.
OVERLAP_SAMPLES: int = 16_000

def _load_whisper_pinned(
    whisper_model_cls: Any,
    model_size: str,
    device: str,
    compute_type: str,
    revision: Optional[str],
) -> Any:
    """Instantiate a faster-whisper ``WhisperModel`` with a pinned revision.

    The ``revision`` kwarg has been part of faster-whisper since 1.0; if
    we ever run against an older build that does not accept it we fall
    back to an unpinned load with a loud warning rather than failing.
    """
    if revision is None:
        return whisper_model_cls(
            model_size, device=device, compute_type=compute_type,
        )
    try:
        return whisper_model_cls(
            model_size,
            device=device,
            compute_type=compute_type,
            revision=revision,
        )
    except TypeError:
        logger.warning(
            "faster-whisper does not accept 'revision' kwarg -- "
            "loading '%s' UNPINNED.  Upgrade faster-whisper to restore "
            "supply-chain pinning.",
            model_size,
        )
        return whisper_model_cls(
            model_size, device=device, compute_type=compute_type,
        )


def _resolve_speaker_label(source: str) -> str:
    """Map a raw source tag from AudioCapture to a display speaker label.

    ``"you"``        -> ``"You"``
    ``"them"``       -> ``"Them"``
    ``"them:Chat"``  -> ``"Them (Chat)"``
    ``"them:Game"``  -> ``"Them (Game)"``
    """
    if source == "you":
        return "You"
    if source == "them":
        return "Them"
    if source.startswith("them:"):
        label = source.split(":", 1)[1]
        return f"Them ({label})"
    return source


class Transcriber:
    """Consumes audio chunks, transcribes them, and emits transcript segments.

    Parameters:
        chunk_queue: A :class:`queue.Queue` of :class:`AudioChunk` objects
            produced by the audio capture layer.
        on_segment: Callback invoked with each :class:`TranscriptSegment`
            that contains usable transcribed text.
        model_size: The faster-whisper model size identifier (e.g.
            ``"large-v3"``, ``"medium"``, ``"distil-large-v3"``), or
            ``"auto"`` to pick by hardware.  Defaults to ``"auto"``.
        device: ``"auto"``, ``"cuda"``, or ``"cpu"``.  ``"auto"`` uses
            GPU if a compatible NVIDIA card is detected, otherwise CPU.
            Defaults to ``"auto"``.
        compute_type: faster-whisper compute type (e.g. ``"float16"``,
            ``"int8"``) or ``"auto"`` to pick by device.  Defaults to
            ``"auto"``.

    Usage::

        q: queue.Queue = queue.Queue()
        transcriber = Transcriber(q, my_callback)
        transcriber.load_model()   # slow -- loads weights onto GPU/CPU
        transcriber.start()        # kicks off the background worker
        ...
        transcriber.stop()         # graceful shutdown
    """

    def __init__(
        self,
        chunk_queue: queue.Queue,
        on_segment: Callable[[TranscriptSegment], Any],
        model_size: str = "auto",
        device: str = "auto",
        compute_type: str = "auto",
    ) -> None:
        self._queue: queue.Queue = chunk_queue
        self._on_segment: Callable[[TranscriptSegment], Any] = on_segment

        # Requested configuration (may contain "auto").
        self._requested_model_size: str = model_size
        self._requested_device: str = device
        self._requested_compute_type: str = compute_type

        # Resolved configuration (populated by load_model).  These reflect
        # the actual device/model the engine is running on -- which may
        # differ from what was requested if we had to fall back.
        self._model_size: str = model_size
        self._device: str = device
        self._compute_type: str = compute_type

        # Model is loaded lazily via load_model().
        self._model: Optional[Any] = None  # faster_whisper.WhisperModel

        # Whether load_model fell back from CUDA to CPU.  Inspected by
        # main.py to notify the user via tray.
        self._fell_back_to_cpu: bool = False

        # Session start time for computing relative timestamps.
        # Set by the caller (main.py) before starting transcription.
        # Defaults to now so early chunks get reasonable offsets even if
        # set_session_start_time() hasn't been called yet.
        self._session_start_time: float = time.time()

        # Worker thread coordination.
        self._stop_event: threading.Event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._drain_on_stop: bool = False

        # Per-source rolling overlap buffers.  Keys are source tags
        # ("you" / "them"), values are 1-D float32 arrays of the last
        # OVERLAP_SAMPLES samples from the previous chunk for that source.
        self._overlap_buffers: Dict[str, np.ndarray] = {}

    # ------------------------------------------------------------------
    # Model lifecycle
    # ------------------------------------------------------------------

    def load_model(self) -> None:
        """Load the faster-whisper model onto the selected device.

        Resolves ``"auto"`` values for device / model size / compute type
        based on detected hardware (see :mod:`tachyon.hardware`), then
        attempts to load.  If CUDA loading fails for any reason (missing
        DLL, no compatible GPU, out-of-memory, etc.), automatically
        retries on CPU with an appropriate CPU-friendly model.

        Each model load is pinned to a specific HuggingFace commit SHA
        (see :mod:`tachyon.model_pins`) so a compromised HF account
        cannot swap the weights without an explicit pin bump.

        This is intentionally a separate, explicit call because model
        loading takes several seconds and the caller may want to show a
        loading indicator while it runs.
        """
        from faster_whisper import WhisperModel  # imported here to keep module import fast

        from tachyon.hardware import (
            HardwareInfo,
            recommend_model_size,
            resolve_transcriber_config,
        )
        from tachyon.model_pins import whisper_revision

        # Resolve "auto" values based on hardware.
        device, model_size, compute_type, hw = resolve_transcriber_config(
            self._requested_device,
            self._requested_model_size,
        )
        # Honour an explicit compute_type override from the user.
        if self._requested_compute_type != "auto":
            compute_type = self._requested_compute_type

        # First attempt -- with the resolved config.
        revision = whisper_revision(model_size)
        try:
            logger.info(
                "Loading faster-whisper model '%s' on %s (compute_type=%s, revision=%s) ...",
                model_size, device, compute_type, revision or "<unpinned>",
            )
            self._model = _load_whisper_pinned(
                WhisperModel, model_size, device, compute_type, revision,
            )
            self._model_size = model_size
            self._device = device
            self._compute_type = compute_type
            logger.info("Model '%s' loaded successfully on %s.", model_size, device)
            return
        except Exception as first_exc:  # noqa: BLE001
            logger.warning(
                "Initial model load failed (device=%s, model=%s): %s",
                device, model_size, first_exc,
            )
            # If we were trying CPU already, there's no fallback.
            if device == "cpu":
                raise

        # ---- Fallback: retry on CPU ----
        # Pick a CPU-friendly model size.  If the user explicitly asked
        # for a particular model, keep it; otherwise recommend a new one.
        if self._requested_model_size == "auto":
            fallback_model = recommend_model_size(
                HardwareInfo(
                    has_cuda=False,
                    cuda_device_name="",
                    vram_gb=0.0,
                    reason="CUDA load failed",
                ),
            )
        else:
            fallback_model = model_size  # honour user's explicit choice

        fallback_compute_type = "int8"
        fallback_revision = whisper_revision(fallback_model)
        logger.info(
            "Falling back to CPU (model=%s, compute_type=%s, revision=%s) after CUDA load failure.",
            fallback_model, fallback_compute_type, fallback_revision or "<unpinned>",
        )
        try:
            self._model = _load_whisper_pinned(
                WhisperModel, fallback_model, "cpu", fallback_compute_type,
                fallback_revision,
            )
            self._model_size = fallback_model
            self._device = "cpu"
            self._compute_type = fallback_compute_type
            self._fell_back_to_cpu = True
            logger.info(
                "Model '%s' loaded successfully on CPU (fallback).",
                fallback_model,
            )
        except Exception:
            logger.exception("CPU fallback load also failed -- cannot continue.")
            raise

    @property
    def model_loaded(self) -> bool:
        """``True`` once :meth:`load_model` has completed successfully."""
        return self._model is not None

    @property
    def device(self) -> str:
        """The resolved compute device -- ``"cuda"`` or ``"cpu"``."""
        return self._device

    @property
    def resolved_model_size(self) -> str:
        """The concrete Whisper model currently loaded."""
        return self._model_size

    @property
    def compute_type(self) -> str:
        """The CTranslate2 compute type currently in use."""
        return self._compute_type

    @property
    def fell_back_to_cpu(self) -> bool:
        """``True`` if CUDA was requested/expected but we fell back to CPU.

        Inspected by main.py after ``load_model()`` to optionally notify
        the user via the tray icon.
        """
        return self._fell_back_to_cpu

    @property
    def model(self) -> Optional[Any]:
        """The underlying faster-whisper ``WhisperModel`` instance.

        Returns ``None`` if the model has not been loaded yet.  Used by
        :class:`BatchTranscriber` to share the same GPU model without
        duplicating VRAM usage.
        """
        return self._model

    def set_session_start_time(self, start_time: Optional[float] = None) -> None:
        """Set the session start time for relative timestamp computation.

        If *start_time* is ``None``, uses the current wall-clock time.
        Should be called before :meth:`start` so that segment timestamps
        are session-relative rather than absolute epoch values.
        """
        self._session_start_time = start_time if start_time is not None else time.time()
        logger.info("Session start time set to %.3f", self._session_start_time)

    # ------------------------------------------------------------------
    # Worker thread start / stop
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background worker thread that consumes audio chunks.

        Raises:
            RuntimeError: If the model has not been loaded yet.
        """
        if not self.model_loaded:
            raise RuntimeError(
                "Cannot start transcriber: model not loaded. "
                "Call load_model() first."
            )
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Transcriber worker is already running.")
            return

        self._stop_event.clear()
        self._drain_on_stop = False
        self._overlap_buffers.clear()
        self._thread = threading.Thread(
            target=self._worker,
            name="TranscriberWorker",
            daemon=True,
        )
        self._thread.start()
        logger.info("Transcriber worker thread started.")

    def stop(self, drain: bool = False) -> None:
        """Signal the worker thread to stop and wait for it to finish.

        Parameters
        ----------
        drain:
            When ``True``, process all already-queued chunks before exiting.
            This is used at recording stop so flushed tail audio is not lost.
        """
        self._drain_on_stop = drain
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            if self._thread.is_alive():
                logger.warning("Transcriber worker did not exit within timeout.")
            else:
                logger.info("Transcriber worker thread stopped.")
            self._thread = None

    # ------------------------------------------------------------------
    # Internal worker
    # ------------------------------------------------------------------

    def _worker(self) -> None:
        """Main loop of the transcription worker thread.

        Continuously pulls :class:`AudioChunk` objects from the queue,
        prepends overlap audio from the previous chunk for the same source,
        runs Whisper transcription, trims the results to only include words
        from the *new* audio region, and emits the result via the callback.
        """
        logger.debug("Transcriber worker entering main loop.")

        while True:
            if self._stop_event.is_set() and not self._drain_on_stop:
                break
            # ----- pull from queue with timeout so stop() is responsive -----
            try:
                chunk: AudioChunk = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._stop_event.is_set():
                    if not self._drain_on_stop or self._queue.empty():
                        break
                continue

            try:
                self._process_chunk(chunk)
            except Exception:
                logger.exception(
                    "Unhandled error while processing audio chunk (source=%s).",
                    chunk.source,
                )

        logger.debug("Transcriber worker exiting main loop.")

    def _process_chunk(self, chunk: AudioChunk) -> None:
        """Transcribe a single audio chunk with overlap handling.

        Steps:
        1. Retrieve (or create) the overlap buffer for this source.
        2. Concatenate overlap + new audio into a combined buffer.
        3. Run faster-whisper transcription on the combined buffer.
        4. Filter words to keep only those whose timestamps fall within the
           new-audio region (i.e., after the overlap duration).
        5. If usable text was found, build a :class:`TranscriptSegment` and
           invoke the ``on_segment`` callback.
        6. Store the tail of the current chunk as the next overlap buffer.
        """
        source: str = chunk.source
        new_audio: np.ndarray = chunk.audio

        if new_audio.size == 0:
            logger.debug("Skipping empty audio chunk (source=%s).", source)
            return

        # --- 1. Get or initialise the overlap buffer for this source ------
        overlap: np.ndarray = self._overlap_buffers.get(
            source,
            np.zeros(0, dtype=np.float32),
        )

        # --- 2. Build the combined buffer (overlap + new audio) -----------
        if overlap.size > 0:
            combined: np.ndarray = np.concatenate([overlap, new_audio])
        else:
            combined = new_audio

        # The number of overlap samples actually prepended (may be < OVERLAP_SAMPLES
        # for the very first chunk of a source).
        overlap_len: int = overlap.size

        # Overlap duration in seconds -- words before this offset are from
        # the previous chunk and must be discarded.
        overlap_duration: float = overlap_len / 16_000.0

        # --- 3. Transcribe the combined buffer ----------------------------
        assert self._model is not None  # guaranteed by start() guard
        segments_iter, _info = self._model.transcribe(
            combined,
            language="en",
            vad_filter=True,
            word_timestamps=True,
        )

        # --- 4. Collect words that fall within the NEW audio portion ------
        kept_words: list[dict[str, Any]] = []

        for segment in segments_iter:
            if segment.words is None:
                continue
            for word in segment.words:
                # word.start / word.end are in seconds relative to the
                # beginning of the combined buffer.  We only keep words
                # whose start falls in the new-audio region.
                if word.start >= overlap_duration:
                    kept_words.append(
                        {
                            "text": word.word,
                            "start": word.start - overlap_duration,
                            "end": word.end - overlap_duration,
                        }
                    )

        # --- 5. Emit a TranscriptSegment if we got any usable text --------
        if kept_words:
            text = "".join(w["text"] for w in kept_words).strip()
            if text:
                speaker = _resolve_speaker_label(source)
                # Compute session-relative timestamps.  chunk.timestamp is
                # absolute time.time(); subtract session start to get offset.
                session_offset = chunk.timestamp - self._session_start_time
                start_time = session_offset + kept_words[0]["start"]
                end_time = session_offset + kept_words[-1]["end"]

                seg = TranscriptSegment(
                    speaker=speaker,
                    text=text,
                    start_time=start_time,
                    end_time=end_time,
                )
                logger.debug(
                    "Segment [%s] %.2fs-%.2fs: %s",
                    speaker,
                    start_time,
                    end_time,
                    text[:80],
                )
                self._on_segment(seg)
        else:
            logger.debug(
                "No speech detected in chunk (source=%s, samples=%d).",
                source,
                new_audio.size,
            )

        # --- 6. Save the tail of the current chunk as the next overlap ----
        if new_audio.size >= OVERLAP_SAMPLES:
            self._overlap_buffers[source] = new_audio[-OVERLAP_SAMPLES:].copy()
        else:
            # Chunk is shorter than the desired overlap -- keep all of it.
            self._overlap_buffers[source] = new_audio.copy()

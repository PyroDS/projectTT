"""Recording session lifecycle and transcript segment storage.

Each recording creates one Session instance that accumulates TranscriptSegment
objects from the transcriber. All segment access is thread-safe via a lock,
since the transcriber worker thread writes segments while the UI thread and
exporter read them.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class WordTiming:
    """Word-level timing within a transcript segment.

    Attributes:
        text: The word text as returned by Whisper (may include leading space).
        start_time: Session-relative start offset in seconds.
        end_time: Session-relative end offset in seconds.
    """

    text: str
    start_time: float
    end_time: float


@dataclass
class TranscriptSegment:
    """A single transcribed speech segment.

    Attributes:
        speaker: Who spoke -- "You" (mic) or "Them" (system audio).
        text: The transcribed text content.
        start_time: Offset in seconds from the session start when speech began.
        end_time: Offset in seconds from the session start when speech ended.
        words: Optional per-word timings for diarization alignment.
    """

    speaker: str
    text: str
    start_time: float
    end_time: float
    words: Optional[list[WordTiming]] = field(default=None, compare=False)


class Session:
    """Manages a single recording session's transcript data.

    Created when the user starts a recording and lives until export is
    complete. Accumulates TranscriptSegment objects emitted by the
    transcriber and provides thread-safe access for the overlay UI and
    the markdown exporter.

    Usage::

        session = Session()
        session.start()

        # From the transcriber callback (worker thread):
        session.add_segment(TranscriptSegment(
            speaker="You",
            text="Hello everyone.",
            start_time=1.2,
            end_time=3.5,
        ))

        # From the UI thread:
        recent = session.get_recent(4)

        # From the exporter (after recording stops):
        all_segments = session.get_all()
        total_seconds = session.duration
    """

    def __init__(self) -> None:
        self._segments: List[TranscriptSegment] = []
        self._lock: threading.Lock = threading.Lock()
        self._start_time: Optional[float] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def start_time(self) -> Optional[float]:
        """Wall-clock time when the session started, or ``None`` if not yet started."""
        return self._start_time

    def start(self) -> None:
        """Mark the session as started, recording the current wall-clock time.

        Must be called before adding segments. Calling start() on an
        already-started session resets the start time (useful if the caller
        needs to restart the clock).
        """
        self._start_time = time.time()

    # ------------------------------------------------------------------
    # Segment access (all thread-safe)
    # ------------------------------------------------------------------

    def add_segment(self, segment: TranscriptSegment) -> None:
        """Append a transcript segment (thread-safe).

        Args:
            segment: The transcribed segment to store.
        """
        with self._lock:
            self._segments.append(segment)

    def get_recent(self, n: int) -> List[TranscriptSegment]:
        """Return the last *n* segments (thread-safe).

        If fewer than *n* segments exist, all segments are returned.

        Args:
            n: Maximum number of recent segments to retrieve.

        Returns:
            A list of the most recent segments (may be shorter than *n*).
        """
        with self._lock:
            return list(self._segments[-n:])

    def get_all(self) -> List[TranscriptSegment]:
        """Return a shallow copy of every segment in order (thread-safe).

        The returned list is a copy, so the caller can iterate without
        holding the lock.

        Returns:
            A new list containing all segments recorded so far.
        """
        with self._lock:
            return list(self._segments)

    # ------------------------------------------------------------------
    # Metadata properties
    # ------------------------------------------------------------------

    @property
    def duration(self) -> float:
        """Elapsed seconds since the session was started.

        Returns ``0.0`` if the session has not been started yet.
        """
        if self._start_time is None:
            return 0.0
        return time.time() - self._start_time

    @property
    def start_datetime(self) -> Optional[datetime]:
        """The wall-clock datetime when the session was started.

        Returns ``None`` if the session has not been started yet.
        """
        if self._start_time is None:
            return None
        return datetime.fromtimestamp(self._start_time)

    @property
    def segment_count(self) -> int:
        """Total number of segments recorded so far (thread-safe)."""
        with self._lock:
            return len(self._segments)

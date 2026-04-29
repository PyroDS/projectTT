"""Markdown transcript exporter.

Creates per-session output folders containing a timestamped Markdown
transcript and an ``audio/`` subfolder for WAV recordings.

Typical output layout::

    output/
    └── 2026-02-22_143000/
        ├── transcript.md
        └── audio/
            ├── mic.wav
            └── system.wav
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Protocol, Sequence

log = logging.getLogger(__name__)
_TRANSCRIPT_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Protocols – decouple from concrete Session / TranscriptSegment classes
# ---------------------------------------------------------------------------

class TranscriptSegmentLike(Protocol):
    """Structural type for a single transcript segment."""

    speaker: str
    text: str
    start_time: float
    end_time: float


class SessionLike(Protocol):
    """Structural type for a recording session."""

    start_datetime: datetime
    duration: float

    def get_all(self) -> Sequence[TranscriptSegmentLike]: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def format_timestamp(seconds: float) -> str:
    """Format a duration in seconds as ``H:MM:SS``.

    Parameters
    ----------
    seconds:
        Non-negative number of seconds.  Fractional parts are truncated
        (floored) so that a segment at 59.9 s renders as ``0:00:59``
        rather than rounding up to the next minute.

    Returns
    -------
    str
        A string of the form ``H:MM:SS``.

    Examples
    --------
    >>> format_timestamp(0)
    '0:00:00'
    >>> format_timestamp(65)
    '0:01:05'
    >>> format_timestamp(3723)
    '1:02:03'
    """
    total_seconds: int = int(seconds)
    hours: int = total_seconds // 3600
    minutes: int = (total_seconds % 3600) // 60
    secs: int = total_seconds % 60
    return f"{hours}:{minutes:02d}:{secs:02d}"


def _build_audio_links(session_dir: Path) -> str:
    """Build the Markdown audio links line from device_manifest.json.

    Reads the manifest to generate links for all WAV files.  Falls back
    to the hardcoded ``mic.wav`` / ``system.wav`` links for old sessions.

    Parameters
    ----------
    session_dir:
        Path to the session folder.

    Returns
    -------
    str
        A Markdown string like ``"**Audio**: [Mic Recording](./audio/mic.wav) | ..."``
    """
    manifest_path = session_dir / "audio" / "device_manifest.json"

    if manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            parts: list[str] = []

            # Mic link (skip if recording had no mic)
            mic_file = data.get("mic", {}).get("file")
            if mic_file:
                parts.append(f"[Mic Recording](./audio/{mic_file})")

            # Loopback links
            loopback_entries = data.get("loopback", [])
            if len(loopback_entries) == 1:
                lb = loopback_entries[0]
                parts.append(f"[System Recording](./audio/{lb['file']})")
            else:
                for lb in loopback_entries:
                    label = lb.get("label", "")
                    display = f"System ({label})" if label else "System"
                    parts.append(f"[{display} Recording](./audio/{lb['file']})")

            return "**Audio**: " + " | ".join(parts)
        except Exception:
            log.warning("Failed to read device manifest, using defaults", exc_info=True)

    # Fallback for old sessions
    return (
        "**Audio**: [Mic Recording](./audio/mic.wav)"
        " | [System Recording](./audio/system.wav)"
    )


def _sidecar_path(markdown_path: Path) -> Path:
    """Return the JSON sidecar path for a markdown transcript file."""
    return markdown_path.with_suffix(".json")


def _serialize_segments(
    segments: Sequence[TranscriptSegmentLike],
) -> list[dict[str, object]]:
    """Serialize transcript segments to JSON-compatible dicts."""
    serialized: list[dict[str, object]] = []
    for idx, segment in enumerate(segments, start=1):
        serialized.append({
            "id": f"seg_{idx:04d}",
            "speaker": segment.speaker,
            "text": segment.text,
            "start": float(segment.start_time),
            "end": float(segment.end_time),
        })
    return serialized


def _write_transcript_sidecar(
    markdown_path: Path,
    segments: Sequence[TranscriptSegmentLike],
    duration_sec: float,
    start_datetime: Optional[datetime],
    source: str,
    version: str = "",
) -> None:
    """Write a JSON sidecar with lossless timing next to markdown output."""
    payload: dict[str, object] = {
        "schema_version": _TRANSCRIPT_SCHEMA_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "duration_sec": float(duration_sec),
        "segments": _serialize_segments(segments),
    }

    if start_datetime is not None:
        payload["start_datetime"] = start_datetime.isoformat(timespec="seconds")
    if version:
        payload["version"] = version

    sidecar = _sidecar_path(markdown_path)
    sidecar.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    log.info("Transcript sidecar written: %s", sidecar.name)


def _load_segments_from_sidecar(path: Path) -> Optional[tuple[dict[str, str], list["_LoadedSegment"]]]:
    """Load transcript data from JSON sidecar if it exists and is valid."""
    sidecar = _sidecar_path(path)
    if not sidecar.exists():
        return None

    from tachyon.session import TranscriptSegment

    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        raw_segments = payload.get("segments", [])
        if not isinstance(raw_segments, list):
            return None

        segments: list[TranscriptSegment] = []
        for item in raw_segments:
            if not isinstance(item, dict):
                continue
            speaker = str(item.get("speaker", "")).strip()
            text = str(item.get("text", "")).strip()
            if not speaker or not text:
                continue
            start = float(item.get("start", 0.0))
            end = float(item.get("end", start))
            if end < start:
                end = start
            segments.append(TranscriptSegment(
                speaker=speaker,
                text=text,
                start_time=start,
                end_time=end,
            ))

        metadata: dict[str, str] = {}
        duration_sec = payload.get("duration_sec")
        if isinstance(duration_sec, (int, float)):
            metadata["duration"] = format_timestamp(float(duration_sec))
        version = payload.get("version")
        if isinstance(version, str) and version.strip():
            metadata["version"] = version.strip()
        start_dt = payload.get("start_datetime")
        if isinstance(start_dt, str) and start_dt.strip():
            try:
                metadata["date"] = datetime.fromisoformat(start_dt).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                metadata["date"] = start_dt

        return metadata, segments
    except Exception:
        log.warning("Failed loading transcript sidecar %s", sidecar, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Core export function
# ---------------------------------------------------------------------------

def export_transcript(session: SessionLike, output_dir: Path) -> Path:
    """Export a completed session to a Markdown transcript on disk.

    Creates a timestamped folder inside *output_dir*, writes
    ``transcript.md``, and prepares an ``audio/`` subfolder where
    ``capture.py`` can later place the WAV recordings.

    Parameters
    ----------
    session:
        A finished recording session exposing ``start_datetime``,
        ``duration``, and ``get_all()`` (list of transcript segments).
    output_dir:
        Root output directory (e.g. ``Path("./output")``).  The function
        creates it if it does not already exist.

    Returns
    -------
    Path
        Absolute path to the created session folder
        (``output_dir / "YYYY-MM-DD_HHMMSS"``).  ``capture.py`` uses
        this to know where to write ``audio/mic.wav`` and
        ``audio/system.wav``.
    """
    # -- Build folder paths ---------------------------------------------------
    folder_name: str = session.start_datetime.strftime("%Y-%m-%d_%H%M%S")
    session_dir: Path = output_dir / folder_name
    audio_dir: Path = session_dir / "audio"

    session_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)

    log.info("Exporting transcript to %s", session_dir)

    # -- Gather data ----------------------------------------------------------
    segments: Sequence[TranscriptSegmentLike] = session.get_all()
    duration_str: str = format_timestamp(session.duration)
    header_date: str = session.start_datetime.strftime("%Y-%m-%d %H:%M")

    # -- Build Markdown -------------------------------------------------------
    lines: list[str] = []

    # Header
    lines.append(f"# Meeting Transcript \u2014 {header_date}")
    lines.append("")
    lines.append(f"**Duration**: {duration_str}")
    lines.append(_build_audio_links(session_dir))
    lines.append("")
    lines.append("---")

    # Segments
    for segment in segments:
        timestamp: str = format_timestamp(segment.start_time)
        lines.append("")
        lines.append(f"**[{timestamp}] {segment.speaker}:**")
        lines.append(segment.text)

    # Ensure file ends with a trailing newline
    lines.append("")

    # -- Write file -----------------------------------------------------------
    transcript_path: Path = session_dir / "transcript.md"
    transcript_path.write_text("\n".join(lines), encoding="utf-8")
    _write_transcript_sidecar(
        markdown_path=transcript_path,
        segments=segments,
        duration_sec=float(session.duration),
        start_datetime=session.start_datetime,
        source="realtime",
        version="v1",
    )

    log.info(
        "Transcript written: %d segments, duration %s",
        len(segments),
        duration_str,
    )

    return session_dir


# ---------------------------------------------------------------------------
# Versioned export — for batch re-transcription
# ---------------------------------------------------------------------------

def discover_versions(session_dir: Path) -> list[str]:
    """Find all transcript markdown files in a session directory.

    Returns a list of filenames sorted by version: ``["transcript.md",
    "transcript_v2.md", "transcript_v3.md", ...]``.  Only files matching
    the expected naming pattern are returned.

    Parameters
    ----------
    session_dir:
        Path to a session folder (e.g. ``output/2026-03-16_135800``).

    Returns
    -------
    list[str]
        Sorted list of transcript filenames found.
    """
    versions: list[str] = []
    for f in session_dir.iterdir():
        if f.name == "transcript.md":
            versions.append(f.name)
        elif re.match(r"^transcript_v\d+\.md$", f.name):
            versions.append(f.name)

    def _sort_key(name: str) -> int:
        if name == "transcript.md":
            return 0
        m = re.search(r"_v(\d+)\.md$", name)
        return int(m.group(1)) if m else 0

    versions.sort(key=_sort_key)
    return versions


def next_version_number(session_dir: Path) -> int:
    """Determine the next version number for a re-transcribed transcript.

    Scans existing transcript files and returns the next sequential version.
    If only ``transcript.md`` exists, returns ``2``.

    Parameters
    ----------
    session_dir:
        Path to a session folder.

    Returns
    -------
    int
        The next version number (>= 2).
    """
    existing = discover_versions(session_dir)
    if not existing:
        return 2

    max_ver = 1  # transcript.md is implicitly v1
    for name in existing:
        m = re.search(r"_v(\d+)\.md$", name)
        if m:
            max_ver = max(max_ver, int(m.group(1)))
    return max_ver + 1


def export_transcript_versioned(
    segments: Sequence[TranscriptSegmentLike],
    session_dir: Path,
    duration: float,
    start_datetime: datetime,
    version: int,
) -> Path:
    """Export a batch-transcribed session as a versioned markdown file.

    Writes ``transcript_v{version}.md`` into *session_dir* with a header
    noting it was re-transcribed in batch mode.

    Parameters
    ----------
    segments:
        The re-transcribed segments in chronological order.
    session_dir:
        The existing session folder (must already exist).
    duration:
        Total session duration in seconds.
    start_datetime:
        When the original recording started.
    version:
        The version number (e.g. 2, 3, ...).

    Returns
    -------
    Path
        Absolute path to the created transcript file.
    """
    duration_str = format_timestamp(duration)
    header_date = start_datetime.strftime("%Y-%m-%d %H:%M")

    lines: list[str] = []
    lines.append(f"# Meeting Transcript \u2014 {header_date}")
    lines.append("")
    lines.append(f"**Duration**: {duration_str}")
    lines.append(f"**Version**: v{version} (Re-transcribed \u2014 batch)")
    lines.append(_build_audio_links(session_dir))
    lines.append("")
    lines.append("---")

    for segment in segments:
        timestamp = format_timestamp(segment.start_time)
        lines.append("")
        lines.append(f"**[{timestamp}] {segment.speaker}:**")
        lines.append(segment.text)

    lines.append("")

    filename = f"transcript_v{version}.md"
    transcript_path = session_dir / filename
    transcript_path.write_text("\n".join(lines), encoding="utf-8")
    _write_transcript_sidecar(
        markdown_path=transcript_path,
        segments=segments,
        duration_sec=float(duration),
        start_datetime=start_datetime,
        source="batch",
        version=f"v{version}",
    )

    log.info(
        "Versioned transcript v%d written: %d segments, duration %s",
        version, len(segments), duration_str,
    )
    return transcript_path


# ---------------------------------------------------------------------------
# Transcript loading — parse markdown back to segments
# ---------------------------------------------------------------------------

# Regex for segment headers: **[H:MM:SS] Speaker:**
_SEGMENT_HEADER_RE = re.compile(
    r"^\*\*\[(\d+:\d{2}:\d{2})\]\s+(.+?):\*\*$"
)


def _parse_timestamp(ts: str) -> float:
    """Parse ``H:MM:SS`` into seconds."""
    parts = ts.split(":")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])


def load_transcript_from_markdown(
    path: Path,
) -> tuple[dict[str, str], list["_LoadedSegment"]]:
    """Load a transcript markdown file back into structured data.

    Parses the header metadata and segment entries from a transcript
    file produced by :func:`export_transcript` or
    :func:`export_transcript_versioned`.

    Parameters
    ----------
    path:
        Path to a ``transcript*.md`` file.

    Returns
    -------
    tuple[dict[str, str], list[_LoadedSegment]]
        A tuple of (metadata_dict, segments).  The metadata dict contains
        keys like ``"duration"`` and ``"date"``.  Segments are simple
        objects with ``speaker``, ``text``, ``start_time``, ``end_time``.
    """
    from tachyon.session import TranscriptSegment

    sidecar_data = _load_segments_from_sidecar(path)
    if sidecar_data is not None:
        metadata, segments = sidecar_data
        if segments:
            return sidecar_data
        # Sidecar exists but has no valid segments — fall back to markdown
        log.warning(
            "Sidecar for %s loaded but contained no valid segments; "
            "falling back to markdown parsing",
            path.name,
        )

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    metadata: dict[str, str] = {}
    segments: list[TranscriptSegment] = []

    current_speaker: Optional[str] = None
    current_time: float = 0.0
    current_text_lines: list[str] = []

    for line in lines:
        # Parse header metadata
        if line.startswith("# Meeting Transcript"):
            m = re.search(r"\u2014\s*(.+)$", line)
            if m:
                metadata["date"] = m.group(1).strip()
            continue

        if line.startswith("**Duration**:"):
            metadata["duration"] = line.split(":", 1)[1].strip()
            continue

        if line.startswith("**Version**:"):
            metadata["version"] = line.split(":", 1)[1].strip()
            continue

        # Check for segment header
        seg_match = _SEGMENT_HEADER_RE.match(line)
        if seg_match:
            # Save previous segment
            if current_speaker is not None:
                segments.append(TranscriptSegment(
                    speaker=current_speaker,
                    text="\n".join(current_text_lines).strip(),
                    start_time=current_time,
                    end_time=current_time,  # best we can do from markdown
                ))

            current_time = _parse_timestamp(seg_match.group(1))
            current_speaker = seg_match.group(2)
            current_text_lines = []
            continue

        # Accumulate text lines for the current segment
        if current_speaker is not None and line.strip():
            current_text_lines.append(line)

    # Don't forget the last segment
    if current_speaker is not None:
        segments.append(TranscriptSegment(
            speaker=current_speaker,
            text="\n".join(current_text_lines).strip(),
            start_time=current_time,
            end_time=current_time,
        ))

    return metadata, segments


# ---------------------------------------------------------------------------
# Diarized export — speaker-identified transcript
# ---------------------------------------------------------------------------

def save_edited_segments(
    path: Path,
    segments: Sequence[TranscriptSegmentLike],
    original_header: str,
) -> None:
    """Overwrite a transcript file with edited segments, preserving the header.

    Reads the original header (everything up to and including the ``---``
    separator) from *original_header* and appends the segments in standard
    ``**[H:MM:SS] Speaker:**`` format.

    Parameters
    ----------
    path:
        Path to the transcript markdown file to overwrite.
    segments:
        The edited segments to write.
    original_header:
        Everything from the start of the file through the ``---`` line
        (inclusive).  Preserved verbatim so duration, version, speaker
        legend, and audio links are unchanged.
    """
    lines: list[str] = [original_header.rstrip("\n")]

    for segment in segments:
        timestamp = format_timestamp(segment.start_time)
        lines.append("")
        lines.append(f"**[{timestamp}] {segment.speaker}:**")
        lines.append(segment.text)

    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    _write_transcript_sidecar(
        markdown_path=path,
        segments=segments,
        duration_sec=max((float(s.end_time) for s in segments), default=0.0),
        start_datetime=None,
        source="edited",
    )

    log.info(
        "Saved edited transcript: %d segments to %s",
        len(segments), path.name,
    )


def export_transcript_diarized(
    segments: Sequence[TranscriptSegmentLike],
    session_dir: Path,
    duration: float,
    start_datetime: datetime,
    version: int,
    speaker_names: dict[str, str] | None = None,
    backend: str = "",
) -> Path:
    """Export a diarized transcript as a versioned markdown file.

    Like :func:`export_transcript_versioned` but adds a "Diarized" label
    in the version header and a speaker legend at the top listing all
    detected speakers and their assigned names.

    Parameters
    ----------
    segments:
        The diarized segments in chronological order.
    session_dir:
        The existing session folder (must already exist).
    duration:
        Total session duration in seconds.
    start_datetime:
        When the original recording started.
    version:
        The version number (e.g. 2, 3, ...).
    speaker_names:
        Optional mapping of speaker_id ("speaker_1") to display name.
        Used for the speaker legend.  If None, the legend uses the
        speaker labels found in the segments.
    backend:
        Embedding backend name (e.g. "speechbrain", "pyannote").
        Included in the version header for traceability.

    Returns
    -------
    Path
        Absolute path to the created transcript file.
    """
    duration_str = format_timestamp(duration)
    header_date = start_datetime.strftime("%Y-%m-%d %H:%M")

    lines: list[str] = []
    lines.append(f"# Meeting Transcript \u2014 {header_date}")
    lines.append("")
    lines.append(f"**Duration**: {duration_str}")
    backend_label = f" \u2014 {backend}" if backend else ""
    lines.append(f"**Version**: v{version} (Diarized{backend_label})")
    lines.append(_build_audio_links(session_dir))
    lines.append("")

    # Build speaker legend from segments
    seen_speakers: dict[str, bool] = {}
    for seg in segments:
        if seg.speaker not in seen_speakers:
            seen_speakers[seg.speaker] = True

    # Only show legend if there are non-"You" speakers
    non_you = [s for s in seen_speakers if s != "You"]
    if non_you:
        lines.append("### Speakers")
        lines.append("")
        if "You" in seen_speakers:
            lines.append("- **You** (microphone)")
        for speaker in non_you:
            # Check if we have a custom name for this speaker
            if speaker_names:
                speaker_num = speaker.split()[-1] if " " in speaker else "0"
                speaker_id = f"speaker_{speaker_num}"
                custom_name = speaker_names.get(speaker_id)
                if custom_name and custom_name != speaker:
                    lines.append(f"- **{speaker}** \u2192 {custom_name}")
                    continue
            # Add source annotation for "Them (Label)" speakers
            if speaker.startswith("Them (") and speaker.endswith(")"):
                lines.append(f"- **{speaker}** (system audio)")
            else:
                lines.append(f"- **{speaker}**")
        lines.append("")

    lines.append("---")

    for segment in segments:
        timestamp = format_timestamp(segment.start_time)
        lines.append("")
        lines.append(f"**[{timestamp}] {segment.speaker}:**")
        lines.append(segment.text)

    lines.append("")

    filename = f"transcript_v{version}.md"
    transcript_path = session_dir / filename
    transcript_path.write_text("\n".join(lines), encoding="utf-8")
    _write_transcript_sidecar(
        markdown_path=transcript_path,
        segments=segments,
        duration_sec=float(duration),
        start_datetime=start_datetime,
        source="diarized",
        version=f"v{version}",
    )

    log.info(
        "Diarized transcript v%d written: %d segments, duration %s",
        version, len(segments), duration_str,
    )
    return transcript_path

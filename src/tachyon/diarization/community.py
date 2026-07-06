"""Community-1 local diarization adapter."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from tachyon.diarization.align import align_segments_to_turns
from tachyon.diarization.community_runtime import (
    load_community_pipeline,
    run_community_pipeline,
)
from tachyon.diarization.sources import (
    load_mono_audio,
    resolve_community_audio_plan,
)
from tachyon.diarization.types import SpeakerTurn
from tachyon.diarizer import (
    DiarizeConfig,
    DiarizeProgress,
    Diarizer,
    SpeakerInfo,
)
from tachyon.exporter import load_transcript_from_markdown
from tachyon.session import TranscriptSegment

logger = logging.getLogger(__name__)


def parse_community_output(output: Any, prefer_exclusive: bool = True) -> list[SpeakerTurn]:
    """Convert pyannote Community-1 output into normalized speaker turns."""
    annotation = None
    if prefer_exclusive and hasattr(output, "exclusive_speaker_diarization"):
        annotation = output.exclusive_speaker_diarization
    if annotation is None and hasattr(output, "speaker_diarization"):
        annotation = output.speaker_diarization

    if annotation is None:
        return []

    turns: list[SpeakerTurn] = []
    if hasattr(annotation, "itertracks"):
        for segment, _track, speaker in annotation.itertracks(yield_label=True):
            turns.append(SpeakerTurn(
                speaker_id=str(speaker),
                start=float(segment.start),
                end=float(segment.end),
            ))
        return turns

    if isinstance(annotation, dict):
        for speaker_id, intervals in annotation.items():
            for start, end in intervals:
                turns.append(SpeakerTurn(
                    speaker_id=str(speaker_id),
                    start=float(start),
                    end=float(end),
                ))
    return sorted(turns, key=lambda turn: turn.start)


def run_community_diarization(
    session_dir: Path,
    source_transcript: str,
    config: DiarizeConfig,
    on_progress: Optional[Callable[[DiarizeProgress], None]] = None,
    stop_event: Optional[Any] = None,
) -> Optional[tuple[list[TranscriptSegment], list[SpeakerInfo]]]:
    """Run the Community-1 local diarization path for a session."""
    def report(phase: str, percent: int, detail: str = "") -> None:
        if on_progress is not None:
            on_progress(DiarizeProgress(phase=phase, percent=percent, detail=detail))

    transcript_path = session_dir / source_transcript
    if not transcript_path.exists():
        logger.error("Source transcript not found: %s", transcript_path)
        return None

    audio_dir = session_dir / "audio"
    report("Loading transcript", 5)
    _, source_segments = load_transcript_from_markdown(transcript_path)
    if not source_segments:
        logger.error("Source transcript has no segments: %s", transcript_path)
        return None

    plan = resolve_community_audio_plan(
        audio_dir,
        source_segments,
        config.audio_mode,
        config.audio_file,
    )
    if plan is None:
        logger.error("No suitable audio found for Community-1 diarization")
        return None

    logger.info(
        "Community-1 audio plan: mode=%s, wav=%s, temp=%s",
        plan.effective_mode,
        plan.wav_path.name,
        plan.temp_file,
    )

    audio = load_mono_audio(plan.wav_path)
    if audio is None or audio.size == 0:
        logger.error("Failed to load Community-1 diarization audio")
        return None

    audio_duration_sec = len(audio) / 16_000
    source_segments = Diarizer._estimate_end_times(source_segments, audio_duration_sec)

    them_segments = [s for s in source_segments if s.speaker != "You"]
    if plan.effective_mode == "system" and len(them_segments) < 2:
        logger.warning("Not enough non-You segments for Community-1 system mode")
        return None
    if plan.effective_mode == "mixed" and len(source_segments) < 2:
        logger.warning("Not enough transcript segments for Community-1 mixed mode")
        return None

    if stop_event is not None and stop_event.is_set():
        return None

    report("Loading Community-1 pipeline", 20, "Downloading/caching model if needed")
    pipeline = load_community_pipeline(config.hf_token)

    if stop_event is not None and stop_event.is_set():
        return None

    report("Running Community-1 diarization", 45, plan.wav_path.name)
    output = run_community_pipeline(
        pipeline,
        audio,
        num_speakers=config.num_speakers,
        min_speakers=config.min_speakers or 2,
        max_speakers=config.max_speakers or 8,
    )

    if stop_event is not None and stop_event.is_set():
        return None

    turns = parse_community_output(output, prefer_exclusive=True)
    if not turns:
        logger.warning("Community-1 returned no speaker turns")
        return None

    num_speakers = len({turn.speaker_id for turn in turns})
    logger.info("Community-1 detected %d speakers across %d turns", num_speakers, len(turns))

    report("Aligning words to speaker turns", 75)
    relabeled = align_segments_to_turns(
        source_segments,
        turns,
        preserve_you=plan.preserve_you,
    )

    if stop_event is not None and stop_event.is_set():
        return None

    report("Merging segments", 85)
    merged = Diarizer._merge_consecutive_segments(
        relabeled,
        max_gap_sec=config.merge_max_gap_sec,
        max_block_sec=config.merge_max_block_sec,
    )

    report("Building speaker profiles", 90)
    diarizer_stub = Diarizer(config=config)
    speaker_info = diarizer_stub._build_speaker_info(merged)

    report("Complete", 100, f"{num_speakers} speakers detected")
    return merged, speaker_info

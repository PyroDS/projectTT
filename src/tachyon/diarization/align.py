"""Word and segment alignment to speaker-turn intervals."""

from __future__ import annotations

from typing import Optional

from tachyon.diarization.types import SpeakerTurn
from tachyon.session import TranscriptSegment, WordTiming


def _overlap(start_a: float, end_a: float, start_b: float, end_b: float) -> float:
    return max(0.0, min(end_a, end_b) - max(start_a, start_b))


def build_speaker_label_map(turns: list[SpeakerTurn]) -> dict[str, str]:
    """Map raw diarization speaker IDs to display labels like ``Speaker 1``."""
    first_seen: dict[str, float] = {}
    for turn in turns:
        if turn.speaker_id not in first_seen:
            first_seen[turn.speaker_id] = turn.start

    ordered = sorted(first_seen.items(), key=lambda item: item[1])
    return {
        speaker_id: f"Speaker {idx + 1}"
        for idx, (speaker_id, _) in enumerate(ordered)
    }


def speaker_id_for_interval(
    start: float,
    end: float,
    turns: list[SpeakerTurn],
    fallback_speaker_id: Optional[str] = None,
) -> Optional[str]:
    """Pick the speaker ID with the greatest overlap for an interval."""
    if not turns:
        return fallback_speaker_id

    best_id: Optional[str] = None
    best_overlap = 0.0
    for turn in turns:
        overlap = _overlap(start, end, turn.start, turn.end)
        if overlap > best_overlap:
            best_overlap = overlap
            best_id = turn.speaker_id

    if best_id is not None and best_overlap > 0:
        return best_id

    midpoint = (start + end) / 2.0
    for turn in turns:
        if turn.start <= midpoint <= turn.end:
            return turn.speaker_id

    return fallback_speaker_id or turns[0].speaker_id


def _segment_from_words(speaker: str, words: list[WordTiming]) -> TranscriptSegment:
    text = "".join(w.text for w in words).strip()
    return TranscriptSegment(
        speaker=speaker,
        text=text,
        start_time=words[0].start_time,
        end_time=words[-1].end_time,
        words=list(words),
    )


def relabel_segment_by_words(
    segment: TranscriptSegment,
    turns: list[SpeakerTurn],
    label_map: dict[str, str],
    fallback_speaker_id: Optional[str] = None,
) -> list[TranscriptSegment]:
    """Split one segment at speaker-change boundaries using word timings."""
    if not segment.words:
        speaker_id = speaker_id_for_interval(
            segment.start_time,
            segment.end_time,
            turns,
            fallback_speaker_id=fallback_speaker_id,
        )
        label = label_map.get(speaker_id or "", "Speaker 1")
        return [TranscriptSegment(
            speaker=label,
            text=segment.text,
            start_time=segment.start_time,
            end_time=segment.end_time,
            words=segment.words,
        )]

    split_segments: list[TranscriptSegment] = []
    current_label: Optional[str] = None
    current_words: list[WordTiming] = []

    for word in segment.words:
        speaker_id = speaker_id_for_interval(
            word.start_time,
            word.end_time,
            turns,
            fallback_speaker_id=fallback_speaker_id,
        )
        speaker_label = label_map.get(speaker_id or "", "Speaker 1")

        if current_label != speaker_label:
            if current_words and current_label is not None:
                split_segments.append(_segment_from_words(current_label, current_words))
            current_label = speaker_label
            current_words = [word]
        else:
            current_words.append(word)

    if current_words and current_label is not None:
        split_segments.append(_segment_from_words(current_label, current_words))

    return split_segments


def align_segments_to_turns(
    segments: list[TranscriptSegment],
    turns: list[SpeakerTurn],
    preserve_you: bool = False,
) -> list[TranscriptSegment]:
    """Relabel transcript segments from Community-1 speaker turns."""
    label_map = build_speaker_label_map(turns)
    fallback_id = turns[0].speaker_id if turns else None
    relabeled: list[TranscriptSegment] = []

    for segment in segments:
        if preserve_you and segment.speaker == "You":
            relabeled.append(segment)
            continue
        relabeled.extend(relabel_segment_by_words(
            segment,
            turns,
            label_map,
            fallback_speaker_id=fallback_id,
        ))

    return relabeled

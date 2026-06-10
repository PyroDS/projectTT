from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from tachyon.exporter import (
    _build_audio_links,
    discover_versions,
    export_transcript_versioned,
    format_timestamp,
    load_transcript_from_markdown,
    next_version_number,
    save_edited_segments,
)
from tachyon.session import Session, TranscriptSegment, WordTiming


def test_format_timestamp_flooring() -> None:
    assert format_timestamp(0) == "0:00:00"
    assert format_timestamp(65) == "0:01:05"
    assert format_timestamp(59.9) == "0:00:59"


def test_discover_versions_and_next_version(tmp_path: Path) -> None:
    (tmp_path / "transcript.md").write_text("", encoding="utf-8")
    (tmp_path / "transcript_v2.md").write_text("", encoding="utf-8")
    (tmp_path / "transcript_v4.md").write_text("", encoding="utf-8")
    (tmp_path / "notes.md").write_text("", encoding="utf-8")

    versions = discover_versions(tmp_path)
    assert versions == ["transcript.md", "transcript_v2.md", "transcript_v4.md"]
    assert next_version_number(tmp_path) == 5


def test_build_audio_links_from_manifest(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir(parents=True)
    manifest = {
        "mic": {"file": "mic.wav"},
        "loopback": [
            {"file": "system_0.wav", "label": "Chat"},
            {"file": "system_1.wav", "label": "Game"},
        ],
    }
    (audio_dir / "device_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    links = _build_audio_links(tmp_path)
    assert "[Mic Recording](./audio/mic.wav)" in links
    assert "[System (Chat) Recording](./audio/system_0.wav)" in links
    assert "[System (Game) Recording](./audio/system_1.wav)" in links


def test_load_transcript_from_markdown_parses_segments(tmp_path: Path) -> None:
    path = tmp_path / "transcript.md"
    path.write_text(
        "\n".join(
            [
                "# Meeting Transcript — 2026-04-12 13:00",
                "",
                "**Duration**: 0:00:10",
                "---",
                "",
                "**[0:00:01] You:**",
                "hello there",
                "",
                "**[0:00:03] Them:**",
                "hi",
                "",
            ]
        ),
        encoding="utf-8",
    )

    metadata, segments = load_transcript_from_markdown(path)
    assert metadata["duration"] == "0:00:10"
    assert len(segments) == 2
    assert segments[0].speaker == "You"
    assert segments[1].text == "hi"


def test_session_contract_for_export_helpers() -> None:
    session = Session()
    session.start()
    session.add_segment(
        TranscriptSegment("You", "test", start_time=0.0, end_time=1.0)
    )
    assert isinstance(session.start_datetime, datetime)
    assert session.get_all()[0].text == "test"


def test_versioned_export_writes_json_sidecar(tmp_path: Path) -> None:
    segments = [
        TranscriptSegment("You", "hello", 0.123, 1.456),
        TranscriptSegment("Them", "hi there", 2.0, 3.25),
    ]
    md_path = export_transcript_versioned(
        segments=segments,
        session_dir=tmp_path,
        duration=9.5,
        start_datetime=datetime(2026, 4, 13, 12, 0, 0),
        version=2,
    )
    sidecar = md_path.with_suffix(".json")
    assert sidecar.exists()

    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["source"] == "batch"
    assert payload["version"] == "v2"
    assert payload["duration_sec"] == 9.5
    assert payload["segments"][0]["start"] == 0.123
    assert payload["segments"][0]["end"] == 1.456


def test_loader_prefers_json_sidecar_precision(tmp_path: Path) -> None:
    md_path = tmp_path / "transcript.md"
    md_path.write_text(
        "\n".join(
            [
                "# Meeting Transcript — 2026-04-13 12:00",
                "",
                "**Duration**: 0:00:10",
                "---",
                "",
                "**[0:00:00] You:**",
                "coarse markdown timestamp",
                "",
            ]
        ),
        encoding="utf-8",
    )
    sidecar_payload = {
        "schema_version": 1,
        "created_at": "2026-04-13T12:00:00",
        "source": "realtime",
        "duration_sec": 10.0,
        "segments": [
            {
                "id": "seg_0001",
                "speaker": "You",
                "text": "precise timing",
                "start": 0.321,
                "end": 1.789,
            }
        ],
    }
    md_path.with_suffix(".json").write_text(
        json.dumps(sidecar_payload), encoding="utf-8"
    )

    _, segments = load_transcript_from_markdown(md_path)
    assert len(segments) == 1
    assert segments[0].text == "precise timing"
    assert segments[0].start_time == 0.321
    assert segments[0].end_time == 1.789


def test_versioned_export_writes_word_metadata_to_sidecar(tmp_path: Path) -> None:
    segments = [
        TranscriptSegment(
            "Them",
            "hello there",
            0.0,
            1.0,
            words=[
                WordTiming("hello ", 0.0, 0.5),
                WordTiming("there", 0.5, 1.0),
            ],
        ),
    ]
    md_path = export_transcript_versioned(
        segments=segments,
        session_dir=tmp_path,
        duration=1.0,
        start_datetime=datetime(2026, 4, 13, 12, 0, 0),
        version=2,
    )

    payload = json.loads(md_path.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["segments"][0]["words"][0]["text"] == "hello "
    assert payload["segments"][0]["words"][1]["start"] == 0.5

    _, loaded = load_transcript_from_markdown(md_path)
    assert loaded[0].words is not None
    assert len(loaded[0].words) == 2
    assert loaded[0].words[1].text == "there"
    assert loaded[0].words[1].start_time == 0.5


def test_save_edited_segments_updates_sidecar(tmp_path: Path) -> None:
    path = tmp_path / "transcript_v3.md"
    original_header = "\n".join(
        [
            "# Meeting Transcript — 2026-04-13 12:00",
            "",
            "**Duration**: 0:00:10",
            "---",
        ]
    )
    segments = [
        TranscriptSegment("Speaker 1", "edited line", 0.0, 2.5),
        TranscriptSegment("Speaker 2", "next line", 2.5, 4.0),
    ]
    save_edited_segments(path, segments, original_header)

    sidecar = path.with_suffix(".json")
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["source"] == "edited"
    assert payload["segments"][1]["speaker"] == "Speaker 2"
    assert payload["segments"][1]["start"] == 2.5


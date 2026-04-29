from __future__ import annotations

import threading

from tachyon.session import Session, TranscriptSegment


def test_session_recent_and_all() -> None:
    session = Session()
    session.start()
    session.add_segment(TranscriptSegment("You", "one", 0.0, 1.0))
    session.add_segment(TranscriptSegment("Them", "two", 1.0, 2.0))
    session.add_segment(TranscriptSegment("You", "three", 2.0, 3.0))

    assert [s.text for s in session.get_recent(2)] == ["two", "three"]
    assert len(session.get_all()) == 3


def test_session_thread_safe_append() -> None:
    session = Session()
    session.start()

    def worker(start: int) -> None:
        for i in range(50):
            session.add_segment(
                TranscriptSegment("You", f"{start+i}", float(i), float(i + 1))
            )

    threads = [threading.Thread(target=worker, args=(i * 100,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert session.segment_count == 200


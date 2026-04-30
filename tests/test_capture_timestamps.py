from __future__ import annotations

import queue

import numpy as np

from tachyon.capture import AudioCapture, TARGET_SAMPLERATE


def test_flush_buffer_sets_chunk_timestamp_to_audio_start(monkeypatch) -> None:
    """Chunk timestamps should represent audio start, not flush time."""
    q: queue.Queue = queue.Queue(maxsize=1)
    capture = AudioCapture(chunk_queue=q)

    # Simulate one full 3-second mic chunk buffered at 16 kHz.
    chunk = np.ones(TARGET_SAMPLERATE * 3, dtype=np.float32)
    capture._mic_buffer = [chunk]  # noqa: SLF001 - unit-testing private helper
    capture._mic_buffer_samples = chunk.size  # noqa: SLF001
    capture._mic_native_sr = TARGET_SAMPLERATE  # noqa: SLF001

    # Flush happens at t=103s. For a 3s chunk, start timestamp should be 100s.
    monkeypatch.setattr("time.time", lambda: 103.0)
    capture._flush_buffer("you")  # noqa: SLF001

    emitted = q.get_nowait()
    assert emitted.source == "you"
    assert emitted.audio.size == chunk.size
    assert emitted.timestamp == 100.0


def test_mic_callback_flushes_using_configured_chunk_duration() -> None:
    q: queue.Queue = queue.Queue(maxsize=4)
    capture = AudioCapture(chunk_queue=q, chunk_duration_sec=1.5)
    capture._mic_native_sr = TARGET_SAMPLERATE  # noqa: SLF001

    half_second = np.ones(TARGET_SAMPLERATE // 2, dtype=np.float32)
    indata = half_second.reshape(-1, 1)

    capture._mic_callback(indata, indata.shape[0], None, None)  # noqa: SLF001
    capture._mic_callback(indata, indata.shape[0], None, None)  # noqa: SLF001
    assert q.empty()

    capture._mic_callback(indata, indata.shape[0], None, None)  # noqa: SLF001
    emitted = q.get_nowait()
    assert emitted.source == "you"
    assert emitted.audio.size == int(TARGET_SAMPLERATE * 1.5)


"""Tests for wall-clock silence padding of loopback WAV files.

WASAPI loopback delivers no frames while nothing plays, so loopback WAVs
must be padded with zeros to keep their timelines aligned with wall clock
(and therefore with mic.wav). See capture.compute_padding_samples.
"""

from __future__ import annotations

import logging
import queue

import numpy as np
import pyaudiowpatch as pyaudio

from tachyon.capture import (
    AudioCapture,
    TARGET_SAMPLERATE,
    _LoopbackState,
    compute_padding_samples,
)


class _StubWavWriter:
    """Records every array written, like a soundfile.SoundFile."""

    def __init__(self) -> None:
        self.writes: list[np.ndarray] = []

    def write(self, data: np.ndarray) -> None:
        self.writes.append(np.asarray(data))

    @property
    def total_samples(self) -> int:
        return sum(len(w) for w in self.writes)


def _make_state(wav_writer=None, native_sr: int = 48_000) -> _LoopbackState:
    return _LoopbackState(
        index=0,
        label="",
        source_tag="them",
        device_info={},
        wav_writer=wav_writer,
        wav_filename="system.wav",
        native_sr=native_sr,
        channels=1,
    )


# ---------------------------------------------------------------------------
# compute_padding_samples (pure helper)
# ---------------------------------------------------------------------------

def test_no_padding_below_threshold() -> None:
    # 0.3 s deficit at 48 kHz stays below the 0.5 s threshold.
    assert compute_padding_samples(100.3, 100.0, 0, 48_000) == 0


def test_padding_equals_deficit_above_threshold() -> None:
    # 68 s gap with nothing written -> exactly 68 s of samples.
    assert compute_padding_samples(168.0, 100.0, 0, 48_000) == 68 * 48_000


def test_padding_is_self_correcting() -> None:
    pad = compute_padding_samples(168.0, 100.0, 0, 48_000)
    assert pad > 0
    # After the pad is written it counts toward samples_written, so an
    # immediate second call reports no further deficit.
    assert compute_padding_samples(168.0, 100.0, pad, 48_000) == 0


def test_continuous_silence_device_is_noop() -> None:
    # Devices that deliver silence frames continuously keep samples_written
    # tracking wall clock exactly -- padding must never trigger.
    start = 100.0
    sr = 48_000
    written = 0
    for step in range(1, 101):
        now = start + step * 0.1
        assert compute_padding_samples(now, start, written, sr) == 0
        written = int((now - start) * sr)


def test_unanchored_stream_never_pads() -> None:
    # stream_start_wall == 0.0 means the stream never started.
    assert compute_padding_samples(168.0, 0.0, 0, 48_000) == 0
    assert compute_padding_samples(168.0, 100.0, 0, 0) == 0


# ---------------------------------------------------------------------------
# _loopback_callback integration
# ---------------------------------------------------------------------------

def test_loopback_callback_pads_gap_before_new_data(monkeypatch) -> None:
    q: queue.Queue = queue.Queue()
    capture = AudioCapture(chunk_queue=q)
    writer = _StubWavWriter()
    state = _make_state(wav_writer=writer)
    state.stream_start_wall = 100.0

    sr = state.native_sr
    frames = sr // 10  # 100 ms of audio
    data = np.full(frames, 0.5, dtype=np.float32).tobytes()

    # First callback shortly after start: below threshold, no padding.
    monkeypatch.setattr("time.time", lambda: 100.1)
    capture._loopback_callback(state, data, frames, None, 0)  # noqa: SLF001
    assert state.padded_samples == 0
    assert state.samples_written == frames

    # Next delivery 10 s later: the gap must be filled with zeros first.
    monkeypatch.setattr("time.time", lambda: 110.0)
    capture._loopback_callback(state, data, frames, None, 0)  # noqa: SLF001

    expected_pad = int(10.0 * sr) - frames
    assert state.padded_samples == expected_pad
    assert state.samples_written == expected_pad + 2 * frames
    assert writer.total_samples == state.samples_written

    # Zeros must land between the two data writes, not after.
    padded_region = np.concatenate(writer.writes[1:-1])
    assert len(padded_region) == expected_pad
    assert not padded_region.any()
    assert writer.writes[-1].max() == np.float32(0.5)


def test_loopback_callback_pads_on_none_data(monkeypatch) -> None:
    q: queue.Queue = queue.Queue()
    capture = AudioCapture(chunk_queue=q)
    writer = _StubWavWriter()
    state = _make_state(wav_writer=writer)
    state.stream_start_wall = 100.0

    monkeypatch.setattr("time.time", lambda: 105.0)
    result = capture._loopback_callback(state, None, 0, None, 0)  # noqa: SLF001

    assert result == (None, pyaudio.paContinue)
    assert state.padded_samples == 5 * state.native_sr
    assert writer.total_samples == 5 * state.native_sr
    # The live chunk buffer must never receive padding.
    assert state.buffer == []
    assert state.buffer_samples == 0


def test_pad_writes_in_bounded_blocks(monkeypatch) -> None:
    # A long gap must be written as multiple <=1s blocks, not one array.
    q: queue.Queue = queue.Queue()
    capture = AudioCapture(chunk_queue=q)
    writer = _StubWavWriter()
    state = _make_state(wav_writer=writer)
    state.stream_start_wall = 100.0

    capture._pad_loopback_wav(state, 220.0)  # noqa: SLF001 - 120 s gap

    assert state.padded_samples == 120 * state.native_sr
    assert all(len(w) <= state.native_sr for w in writer.writes)


# ---------------------------------------------------------------------------
# Mic diagnostic guard
# ---------------------------------------------------------------------------

def test_mic_gap_warns_once_and_writes_nothing(monkeypatch, caplog) -> None:
    q: queue.Queue = queue.Queue()
    capture = AudioCapture(chunk_queue=q)
    capture._mic_native_sr = TARGET_SAMPLERATE  # noqa: SLF001
    capture._mic_stream_start_wall = 100.0  # noqa: SLF001

    frames = TARGET_SAMPLERATE // 2
    indata = np.zeros(frames, dtype=np.float32).reshape(-1, 1)

    monkeypatch.setattr("time.time", lambda: 110.0)
    with caplog.at_level(logging.WARNING, logger="tachyon.capture"):
        capture._mic_callback(indata, frames, None, None)  # noqa: SLF001
        capture._mic_callback(indata, frames, None, None)  # noqa: SLF001

    warnings = [r for r in caplog.records if "fell behind wall clock" in r.message]
    assert len(warnings) == 1
    # Diagnostic only: no WAV writer exists, nothing is padded.
    assert capture._mic_wav is None  # noqa: SLF001


def test_mic_callback_without_anchor_never_warns(caplog) -> None:
    # Sessions where start() was not called (unit tests, mic-less) must not warn.
    q: queue.Queue = queue.Queue()
    capture = AudioCapture(chunk_queue=q)
    capture._mic_native_sr = TARGET_SAMPLERATE  # noqa: SLF001

    frames = TARGET_SAMPLERATE // 2
    indata = np.zeros(frames, dtype=np.float32).reshape(-1, 1)
    with caplog.at_level(logging.WARNING, logger="tachyon.capture"):
        capture._mic_callback(indata, frames, None, None)  # noqa: SLF001

    assert not [r for r in caplog.records if "fell behind" in r.message]

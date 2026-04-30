"""WASAPI audio capture for microphone and system (loopback) audio.

Manages parallel audio streams:
  - Microphone stream  ("you")  -- captures the local user's voice.
  - Loopback stream(s) ("them") -- captures system/meeting audio via WASAPI
                                    loopback on one or more output devices.

Audio is captured at each device's native sample rate, resampled to 16 kHz
mono float32 via soxr, and placed into a shared ``queue.Queue`` as
``AudioChunk`` objects for the transcriber to consume.  Full-session audio
is simultaneously written to WAV files on disk.

Multi-loopback support
----------------------
When multiple loopback devices are configured:
  - Each gets its own PyAudioWPatch stream and WAV file
  - Single loopback:  ``system.wav``,  source tag ``"them"``
  - N>1 loopbacks:    ``system_0.wav``, ``system_1.wav``, ...
                      source tags ``"them:Chat"``, ``"them:Game"``, ...
  - A ``device_manifest.json`` is written to the audio/ dir mapping
    filenames to device labels

If loopback capture fails for a device, the remaining devices continue.
"""

from __future__ import annotations

import json
import logging
import queue
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pyaudiowpatch as pyaudio
import sounddevice as sd
import soundfile as sf
import soxr

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TARGET_SAMPLERATE: int = 16_000       # Whisper expects 16 kHz
CHUNK_DURATION_SEC: float = 3.0       # ~3 s of audio per queued chunk
WASAPI_HOSTAPI_NAME: str = "Windows WASAPI"


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class AudioChunk:
    """A chunk of captured audio ready for transcription.

    Attributes:
        source: ``"you"`` for microphone audio, ``"them"`` for system/loopback
                (single device), or ``"them:Label"`` for multi-loopback.
        audio:  1-D numpy array of float32 samples at 16 kHz mono.
        timestamp: Wall-clock ``time.time()`` corresponding to the start of
            this chunk's audio (not enqueue/flush time).
    """

    source: str          # "you" | "them" | "them:Label"
    audio: np.ndarray    # float32, 16 kHz, mono
    timestamp: float     # wall-clock start time of chunk audio


@dataclass
class _LoopbackState:
    """Internal state for one loopback capture stream.

    Attributes:
        index: Index in the loopback states list.
        label: User-defined label (e.g. "Chat", "Game").
        source_tag: Source tag for AudioChunks (e.g. "them" or "them:Chat").
        device_info: PyAudioWPatch device info dict for the loopback device.
        stream: The active PyAudioWPatch stream, or None.
        wav_writer: The SoundFile WAV writer, or None.
        wav_filename: Just the filename (e.g. "system.wav" or "system_0.wav").
        native_sr: Device native sample rate.
        channels: Number of channels on the device.
        buffer: Accumulated audio blocks before flushing.
        buffer_samples: Total sample count in the buffer.
        active: Whether this stream is running.
    """

    index: int
    label: str
    source_tag: str
    device_info: Dict[str, Any]
    stream: Optional[Any] = None
    wav_writer: Optional[sf.SoundFile] = None
    wav_filename: str = ""
    native_sr: int = 0
    channels: int = 0
    buffer: list = field(default_factory=list)
    buffer_samples: int = 0
    active: bool = False


# ---------------------------------------------------------------------------
# AudioCapture
# ---------------------------------------------------------------------------

class AudioCapture:
    """Captures microphone and system audio via WASAPI.

    Parameters:
        chunk_queue:    Thread-safe queue shared with the transcriber.  Each
                        item placed in the queue is an ``AudioChunk``.
        mic_device:     Name (substring match) or integer index of the desired
                        WASAPI input device.  ``None`` uses the system default.
        output_device:  Name (substring match) or integer index of the desired
                        WASAPI output device for loopback capture.  ``None``
                        uses the system default output.  Ignored when
                        *loopback_configs* is provided.
        loopback_configs: Optional list of ``LoopbackDevice`` objects for
                        multi-loopback capture.  Falls back to *output_device*
                        if None or empty.
    """

    def __init__(
        self,
        chunk_queue: queue.Queue,
        mic_device: Optional[str | int] = None,
        output_device: Optional[str | int] = None,
        loopback_configs: Optional[list] = None,
    ) -> None:
        self._chunk_queue = chunk_queue
        self._mic_device_hint = mic_device
        self._output_device_hint = output_device
        self._loopback_configs = loopback_configs or []

        # Stream handles (set in start())
        self._mic_stream: Optional[sd.InputStream] = None
        self._pyaudio: Optional[pyaudio.PyAudio] = None
        self._mic_active: bool = False

        # WAV writer for mic
        self._mic_wav: Optional[sf.SoundFile] = None

        # Multi-loopback state
        self._loopback_states: list[_LoopbackState] = []

        # Accumulation buffer for mic
        self._mic_buffer: list[np.ndarray] = []
        self._mic_buffer_samples: int = 0
        self._mic_native_sr: int = 0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def mic_active(self) -> bool:
        """Whether the microphone stream is currently running."""
        return self._mic_active

    @property
    def loopback_active(self) -> bool:
        """Whether at least one WASAPI loopback stream is currently running."""
        return any(s.active for s in self._loopback_states)

    @property
    def active_loopback_count(self) -> int:
        """Number of currently active loopback streams."""
        return sum(1 for s in self._loopback_states if s.active)

    @property
    def loopback_info(self) -> list[dict[str, Any]]:
        """Return details of active loopback devices."""
        return [
            {
                "label": s.label,
                "source_tag": s.source_tag,
                "device_name": s.device_info.get("name", ""),
                "wav_filename": s.wav_filename,
            }
            for s in self._loopback_states if s.active
        ]

    # ------------------------------------------------------------------
    # Device enumeration
    # ------------------------------------------------------------------

    @staticmethod
    def get_devices() -> List[Dict[str, Any]]:
        """Return a list of available WASAPI audio devices.

        Each item is a dict from ``sounddevice.query_devices()`` with an
        added ``"index"`` key.  Only devices belonging to the Windows WASAPI
        host API are included.
        """
        hostapis = sd.query_hostapis()
        wasapi_index: Optional[int] = None
        for idx, api in enumerate(hostapis):
            if WASAPI_HOSTAPI_NAME in api["name"]:
                wasapi_index = idx
                break

        if wasapi_index is None:
            logger.warning("WASAPI host API not found among: %s",
                           [api["name"] for api in hostapis])
            return []

        all_devices = sd.query_devices()
        wasapi_devices: List[Dict[str, Any]] = []
        for i, dev in enumerate(all_devices):
            if dev["hostapi"] == wasapi_index:
                entry = dict(dev)
                entry["index"] = i
                wasapi_devices.append(entry)

        logger.info("Found %d WASAPI devices", len(wasapi_devices))
        return wasapi_devices

    @staticmethod
    def get_loopback_devices() -> List[Dict[str, Any]]:
        """Return a list of WASAPI output devices available for loopback.

        Each item has ``"name"`` and ``"index"`` keys from PyAudioWPatch's
        loopback device enumeration.  Used by the tray UI for device selection.
        """
        devices: List[Dict[str, Any]] = []
        try:
            pa = pyaudio.PyAudio()
            try:
                for lb in pa.get_loopback_device_info_generator():
                    devices.append(dict(lb))
            finally:
                pa.terminate()
        except Exception:
            logger.warning("Failed to enumerate loopback devices", exc_info=True)
        return devices

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    def start(self, audio_dir: Path) -> None:
        """Start microphone and loopback capture, writing WAV files.

        Args:
            audio_dir: Directory to write WAV files and device_manifest.json.
                       Created if it does not exist.
        """
        audio_dir = Path(audio_dir)
        audio_dir.mkdir(parents=True, exist_ok=True)

        # Reset buffers
        self._mic_buffer = []
        self._mic_buffer_samples = 0
        self._loopback_states = []
        self._mic_active = False

        # Resolve devices ------------------------------------------------
        wasapi_devices = self.get_devices()
        mic_index = self._resolve_device(
            self._mic_device_hint, wasapi_devices, kind="input"
        )

        # WASAPI extra settings (shared-mode, not exclusive) ---------------
        wasapi_settings = sd.WasapiSettings(exclusive=False)

        # --- Microphone stream (optional) --------------------------------
        mic_info: Optional[Dict[str, Any]] = None
        if mic_index is None:
            logger.warning(
                "No microphone available — recording system audio only. "
                "Configured mic hint: %r",
                self._mic_device_hint,
            )
        else:
            try:
                mic_info = sd.query_devices(mic_index)
                self._mic_native_sr = int(mic_info["default_samplerate"])

                logger.info(
                    "Opening mic stream: device=%d (%s), sr=%d Hz",
                    mic_index, mic_info["name"], self._mic_native_sr,
                )

                self._mic_wav = sf.SoundFile(
                    str(audio_dir / "mic.wav"),
                    mode="w",
                    samplerate=self._mic_native_sr,
                    channels=1,
                    subtype="PCM_16",
                )

                self._mic_stream = sd.InputStream(
                    device=mic_index,
                    samplerate=self._mic_native_sr,
                    channels=1,
                    dtype="float32",
                    callback=self._mic_callback,
                    extra_settings=wasapi_settings,
                )
                self._mic_stream.start()
                self._mic_active = True
                logger.info("Mic stream started successfully")

            except Exception:
                logger.warning(
                    "Failed to open microphone stream — continuing without mic",
                    exc_info=True,
                )
                # Clean up any partial resources
                if self._mic_wav is not None:
                    try:
                        self._mic_wav.close()
                    except Exception:
                        pass
                    self._mic_wav = None
                self._mic_stream = None
                self._mic_active = False
                mic_info = None

        # --- Loopback stream(s) (via PyAudioWPatch) -----------------------
        self._pyaudio = pyaudio.PyAudio()

        # Build the list of loopback targets from configs or fallback
        loopback_targets = self._resolve_loopback_targets()

        # Determine naming: single = system.wav, multi = system_N.wav
        multi = len(loopback_targets) > 1

        for i, (lb_device, label) in enumerate(loopback_targets):
            source_tag = f"them:{label}" if multi and label else "them" if not multi else f"them:{label or str(i)}"
            wav_filename = f"system_{i}.wav" if multi else "system.wav"

            state = _LoopbackState(
                index=i,
                label=label,
                source_tag=source_tag,
                device_info=lb_device,
                wav_filename=wav_filename,
                native_sr=int(lb_device["defaultSampleRate"]),
                channels=max(int(lb_device["maxInputChannels"]), 1),
            )

            try:
                logger.info(
                    "Opening loopback stream %d: device=%d (%s), sr=%d Hz, ch=%d, tag=%s",
                    i, lb_device["index"], lb_device["name"],
                    state.native_sr, state.channels, source_tag,
                )

                state.wav_writer = sf.SoundFile(
                    str(audio_dir / wav_filename),
                    mode="w",
                    samplerate=state.native_sr,
                    channels=1,
                    subtype="PCM_16",
                )

                # Create a per-stream callback via closure
                def _make_callback(st: _LoopbackState):
                    def _cb(in_data, frame_count, time_info, status_flags):
                        return self._loopback_callback(st, in_data, frame_count, time_info, status_flags)
                    return _cb

                state.stream = self._pyaudio.open(
                    format=pyaudio.paFloat32,
                    channels=state.channels,
                    rate=state.native_sr,
                    input=True,
                    input_device_index=lb_device["index"],
                    frames_per_buffer=int(state.native_sr * 0.1),  # 100 ms
                    stream_callback=_make_callback(state),
                )
                state.stream.start_stream()
                state.active = True
                logger.info("Loopback stream %d started: %s", i, lb_device["name"])

            except Exception:
                logger.warning(
                    "Failed to open loopback stream %d (%s) — skipping",
                    i, lb_device.get("name", "unknown"),
                    exc_info=True,
                )
                # Clean up partial resources for this stream — close the
                # PyAudio stream handle first (if opened) before the
                # WAV, otherwise the audio device stays reserved.
                if state.stream is not None:
                    try:
                        state.stream.close()
                    except Exception:
                        logger.debug("Error closing partial loopback stream", exc_info=True)
                    state.stream = None
                if state.wav_writer is not None:
                    try:
                        state.wav_writer.close()
                    except Exception:
                        logger.debug("Error closing partial loopback WAV", exc_info=True)
                    state.wav_writer = None
                state.active = False

            self._loopback_states.append(state)

        active_count = self.active_loopback_count
        if active_count == 0 and loopback_targets:
            logger.warning("No loopback streams could be started")
        else:
            logger.info("%d loopback stream(s) active", active_count)

        # Bail out only if we have no audio sources at all
        if not self._mic_active and active_count == 0:
            raise RuntimeError(
                "No audio sources available — recording cannot start. "
                "Check microphone and loopback device settings."
            )

        # Write device manifest
        self._write_device_manifest(
            audio_dir, mic_info["name"] if mic_info else None,
        )

    def stop(self) -> None:
        """Stop all capture streams and close WAV files."""
        # Stop mic stream
        if self._mic_stream is not None:
            try:
                self._mic_stream.stop()
                self._mic_stream.close()
                logger.info("Mic stream stopped")
            except Exception:
                logger.warning("Error stopping mic stream", exc_info=True)
            self._mic_stream = None

        # Stop all loopback streams
        for state in self._loopback_states:
            if state.stream is not None:
                try:
                    state.stream.stop_stream()
                    state.stream.close()
                    logger.info("Loopback stream %d stopped", state.index)
                except Exception:
                    logger.warning("Error stopping loopback stream %d", state.index, exc_info=True)
                state.stream = None
                state.active = False

        if self._pyaudio is not None:
            try:
                self._pyaudio.terminate()
            except Exception:
                logger.warning("Error terminating PyAudio", exc_info=True)
            self._pyaudio = None

        # Flush remaining buffered audio before closing WAV files
        self._flush_buffer("you")
        for state in self._loopback_states:
            self._flush_loopback_buffer(state)

        # Close mic WAV
        if self._mic_wav is not None:
            try:
                self._mic_wav.close()
                logger.info("mic.wav closed")
            except Exception:
                logger.warning("Error closing mic.wav", exc_info=True)
            self._mic_wav = None

        # Close loopback WAVs
        for state in self._loopback_states:
            if state.wav_writer is not None:
                try:
                    state.wav_writer.close()
                    logger.info("%s closed", state.wav_filename)
                except Exception:
                    logger.warning("Error closing %s", state.wav_filename, exc_info=True)
                state.wav_writer = None

    # ------------------------------------------------------------------
    # Sounddevice callbacks (run in separate C-level threads)
    # ------------------------------------------------------------------

    def _mic_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: Any,
        status: sd.CallbackFlags,
    ) -> None:
        """Sounddevice callback for the microphone stream."""
        if status:
            logger.debug("Mic stream status: %s", status)

        # indata shape: (frames, channels) -- take channel 0 for mono
        mono = indata[:, 0].copy()

        # Write raw audio to WAV file at native sample rate
        if self._mic_wav is not None:
            try:
                self._mic_wav.write(mono)
            except Exception:
                logger.warning("Error writing to mic.wav", exc_info=True)

        # Accumulate into buffer
        self._mic_buffer.append(mono)
        self._mic_buffer_samples += len(mono)

        samples_per_chunk = int(self._mic_native_sr * CHUNK_DURATION_SEC)
        if self._mic_buffer_samples >= samples_per_chunk:
            self._flush_buffer("you")

    def _loopback_callback(
        self,
        state: _LoopbackState,
        in_data: Optional[bytes],
        frame_count: int,
        time_info: Any,
        status_flags: int,
    ) -> tuple[None, int]:
        """PyAudioWPatch callback for a loopback stream (per-state closure)."""
        if status_flags:
            logger.debug("Loopback stream %d status flags: %s", state.index, status_flags)

        if in_data is None:
            return (None, pyaudio.paContinue)

        # Convert raw bytes → float32 numpy, reshape to (frames, channels)
        audio = np.frombuffer(in_data, dtype=np.float32)
        if state.channels > 1:
            audio = audio.reshape(-1, state.channels)
            mono = audio.mean(axis=1).astype(np.float32)
        else:
            mono = audio.copy()

        # Write raw audio to WAV file at native sample rate
        if state.wav_writer is not None:
            try:
                state.wav_writer.write(mono)
            except Exception:
                logger.warning("Error writing to %s", state.wav_filename, exc_info=True)

        # Accumulate into buffer
        state.buffer.append(mono)
        state.buffer_samples += len(mono)

        samples_per_chunk = int(state.native_sr * CHUNK_DURATION_SEC)
        if state.buffer_samples >= samples_per_chunk:
            self._flush_loopback_buffer(state)

        return (None, pyaudio.paContinue)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _flush_buffer(self, source: str) -> None:
        """Concatenate accumulated mic buffer, resample to 16 kHz, and enqueue.

        Args:
            source: Must be ``"you"`` (mic buffer only — loopback uses
                    ``_flush_loopback_buffer``).
        """
        buf = self._mic_buffer
        native_sr = self._mic_native_sr
        self._mic_buffer = []
        self._mic_buffer_samples = 0

        if not buf or native_sr == 0:
            return

        raw_audio = np.concatenate(buf)

        if native_sr != TARGET_SAMPLERATE:
            resampled = soxr.resample(raw_audio, native_sr, TARGET_SAMPLERATE)
        else:
            resampled = raw_audio

        resampled = resampled.astype(np.float32, copy=False)

        # ``timestamp`` is defined as the wall-clock start of this chunk's
        # audio so downstream timestamp math stays aligned to speech onset.
        chunk_start = time.time() - (len(resampled) / TARGET_SAMPLERATE)
        chunk = AudioChunk(
            source=source,
            audio=resampled,
            timestamp=chunk_start,
        )

        try:
            self._chunk_queue.put(chunk, timeout=2.0)
        except queue.Full:
            logger.warning("Chunk queue full after 2 s — dropping %s chunk", source)

    def _flush_loopback_buffer(self, state: _LoopbackState) -> None:
        """Concatenate accumulated loopback buffer, resample, and enqueue."""
        buf = state.buffer
        native_sr = state.native_sr
        state.buffer = []
        state.buffer_samples = 0

        if not buf or native_sr == 0:
            return

        raw_audio = np.concatenate(buf)

        if native_sr != TARGET_SAMPLERATE:
            resampled = soxr.resample(raw_audio, native_sr, TARGET_SAMPLERATE)
        else:
            resampled = raw_audio

        resampled = resampled.astype(np.float32, copy=False)

        # ``timestamp`` is defined as the wall-clock start of this chunk's
        # audio so downstream timestamp math stays aligned to speech onset.
        chunk_start = time.time() - (len(resampled) / TARGET_SAMPLERATE)
        chunk = AudioChunk(
            source=state.source_tag,
            audio=resampled,
            timestamp=chunk_start,
        )

        try:
            self._chunk_queue.put(chunk, timeout=2.0)
        except queue.Full:
            logger.warning("Chunk queue full after 2 s — dropping %s chunk", state.source_tag)

    def _resolve_loopback_targets(self) -> list[tuple[Dict[str, Any], str]]:
        """Resolve loopback configs to (pyaudio_device_info, label) pairs.

        Falls back to the legacy output_device hint or WASAPI default.
        Returns a list of (device_info_dict, label_string) tuples.
        """
        if self._pyaudio is None:
            return []

        # Collect all loopback devices from PyAudioWPatch for matching
        all_loopbacks = list(self._pyaudio.get_loopback_device_info_generator())

        targets: list[tuple[Dict[str, Any], str]] = []

        if self._loopback_configs:
            # Multi-loopback from LoopbackDevice configs
            available_names = [lb["name"] for lb in all_loopbacks]
            for cfg in self._loopback_configs:
                lb = self._find_loopback_device(all_loopbacks, cfg.device_name)
                if lb is not None:
                    targets.append((lb, cfg.label))
                else:
                    logger.warning(
                        "Could not find loopback device for '%s' (label: %s) — "
                        "skipping. Available: %s",
                        cfg.device_name, cfg.label, available_names,
                    )
            # If every explicit config failed to resolve, fall back to the
            # WASAPI system default loopback so the user still gets something
            # (covers disconnected devices and Windows ordinal-prefix renames
            # like "Arctis 7 Chat" → "2- Arctis 7 Chat").
            if not targets:
                lb = self._find_default_loopback(all_loopbacks)
                if lb is not None:
                    logger.warning(
                        "No configured loopback devices resolved — falling "
                        "back to WASAPI default: %s",
                        lb.get("name", "?"),
                    )
                    targets.append((lb, ""))
        else:
            # No explicit configs: use system default output for loopback.
            lb = self._find_default_loopback(all_loopbacks)
            if lb is not None:
                targets.append((lb, ""))

        return targets

    def _find_loopback_device(
        self,
        all_loopbacks: list[Dict[str, Any]],
        hint: Optional[str | int],
    ) -> Optional[Dict[str, Any]]:
        """Find a PyAudioWPatch loopback device matching a hint.

        Args:
            all_loopbacks: Pre-enumerated list of loopback device dicts.
            hint: Device name (substring match) or None for default.

        Returns:
            Matching loopback device dict, or None.
        """
        if hint is None:
            return self._find_default_loopback(all_loopbacks)

        if isinstance(hint, str):
            hint_lower = hint.lower()
            for lb in all_loopbacks:
                if hint_lower in lb["name"].lower():
                    return lb

        if isinstance(hint, int):
            output_info = sd.query_devices(hint)
            output_name: str = output_info["name"]
            for lb in all_loopbacks:
                if output_name in lb["name"]:
                    return lb

        return None

    def _find_default_loopback(
        self,
        all_loopbacks: list[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Find the default WASAPI output device's loopback."""
        if self._pyaudio is None:
            return None
        try:
            wasapi_info = self._pyaudio.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_output = self._pyaudio.get_device_info_by_index(
                wasapi_info["defaultOutputDevice"]
            )
            default_name: str = default_output["name"]
            for lb in all_loopbacks:
                if default_name in lb["name"]:
                    return lb
        except Exception:
            logger.warning("Could not determine default loopback device", exc_info=True)
        return None

    def _write_device_manifest(
        self,
        audio_dir: Path,
        mic_device_name: Optional[str],
    ) -> None:
        """Write device_manifest.json to the audio directory.

        Maps filenames to device labels for downstream consumers
        (batch transcriber, diarizer, exporter).

        ``mic_device_name`` is ``None`` when recording without a microphone;
        in that case the manifest omits the mic.wav entry entirely so that
        downstream ``data.get("mic", {}).get("file")`` calls still work.
        """
        manifest: Dict[str, Any] = {"loopback": []}
        if mic_device_name is not None:
            manifest["mic"] = {"file": "mic.wav", "device": mic_device_name}

        for state in self._loopback_states:
            if state.active:
                manifest["loopback"].append({
                    "file": state.wav_filename,
                    "label": state.label,
                    "device": state.device_info.get("name", ""),
                    "source_tag": state.source_tag,
                })

        manifest_path = audio_dir / "device_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.info("Device manifest written: %s", manifest_path)

    def _resolve_device(
        self,
        hint: Optional[str | int],
        wasapi_devices: List[Dict[str, Any]],
        kind: str,
    ) -> Optional[int]:
        """Resolve a user-provided device hint to a sounddevice index.

        Args:
            hint:           A device name (substring match, case-insensitive)
                            or integer index.  ``None`` means "use the system
                            default".
            wasapi_devices: Pre-filtered list of WASAPI device dicts from
                            ``get_devices()``.
            kind:           ``"input"`` or ``"output"`` -- used to pick the
                            correct system default when *hint* is None.

        Returns:
            An integer device index suitable for ``sounddevice.InputStream``,
            or ``None`` if no matching device could be found. Callers decide
            whether a missing device is fatal (see ``start()`` for the
            mic-optional / loopback-optional policy).
        """
        # --- Explicit integer index --------------------------------------
        if isinstance(hint, int):
            logger.info("Using explicit device index %d for %s", hint, kind)
            return hint

        # --- Name substring match ----------------------------------------
        if isinstance(hint, str) and hint:
            hint_lower = hint.lower()
            for dev in wasapi_devices:
                if hint_lower in dev["name"].lower():
                    logger.info(
                        "Matched %s device '%s' (index %d) for hint '%s'",
                        kind, dev["name"], dev["index"], hint,
                    )
                    return dev["index"]

            # No match found — log available options and return None so the
            # caller can decide how to handle it.
            available = [d["name"] for d in wasapi_devices]
            logger.warning(
                "No WASAPI %s device matching %r. Available: %s",
                kind, hint, available,
            )
            return None

        # --- System default -----------------------------------------------
        try:
            defaults = sd.query_devices(kind=kind)
            default_index = defaults["index"] if isinstance(defaults, dict) else int(defaults)
        except Exception:
            # Fallback: query the default device indices tuple
            default_input, default_output = sd.default.device
            default_index = default_input if kind == "input" else default_output

        # Verify the default is actually a WASAPI device.  If not, find
        # the WASAPI device with a matching name (the same physical device
        # appears under multiple host APIs with different indices).
        wasapi_indices = {d["index"] for d in wasapi_devices}
        if default_index not in wasapi_indices:
            key = "max_input_channels" if kind == "input" else "max_output_channels"
            default_name: str = sd.query_devices(default_index)["name"]

            # First pass: find a WASAPI device whose name matches the default
            for dev in wasapi_devices:
                if dev.get(key, 0) > 0 and default_name in dev["name"]:
                    logger.info(
                        "System default device (%d, '%s') is not WASAPI; "
                        "matched WASAPI device '%s' (index %d) by name for %s",
                        default_index, default_name,
                        dev["name"], dev["index"], kind,
                    )
                    return dev["index"]

            # Second pass: no name match — fall back to first WASAPI device
            for dev in wasapi_devices:
                if dev.get(key, 0) > 0:
                    logger.warning(
                        "System default device (%d, '%s') is not WASAPI and "
                        "no name match found; falling back to WASAPI device "
                        "'%s' (index %d) for %s",
                        default_index, default_name,
                        dev["name"], dev["index"], kind,
                    )
                    return dev["index"]

            # Nothing suitable found
            logger.warning(
                "No suitable WASAPI %s device found. Available: %s",
                kind, [d["name"] for d in wasapi_devices],
            )
            return None

        dev_info = sd.query_devices(default_index)
        logger.info(
            "Using default %s device: '%s' (index %d)",
            kind, dev_info["name"], default_index,
        )
        return default_index

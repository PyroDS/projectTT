"""Tachyon Transcripts — main entry point.

Wires all components together: config, audio capture, transcription,
session management, markdown export, system tray, and caption overlay.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# CUDA DLL discovery — nvidia pip packages install cublas/cudnn DLLs into
# subdirectories of site-packages that are NOT on the default DLL search
# path.  We must register them before any CUDA library is loaded.
# ---------------------------------------------------------------------------
def _register_cuda_dll_dirs() -> None:
    """Register possible CUDA DLL directories for source and frozen installs.

    Source installs keep CUDA DLLs under ``site-packages/nvidia/*/(bin|lib)``.
    Frozen installs place DLLs in one-folder bundle paths such as
    ``_internal/cuda``. We register all plausible directories before any
    CTranslate2/faster-whisper import path can resolve CUDA libraries.
    """
    if sys.platform != "win32":
        return

    candidate_dirs: list[Path] = []
    seen: set[str] = set()

    def _add(path: Path) -> None:
        try:
            resolved = str(path.resolve())
        except OSError:
            resolved = str(path)
        lowered = resolved.lower()
        if lowered in seen:
            return
        seen.add(lowered)
        candidate_dirs.append(path)

    exe_dir = Path(sys.executable).resolve().parent
    _add(exe_dir)
    _add(exe_dir / "_internal")
    _add(exe_dir / "_internal" / "cuda")

    meipass = getattr(sys, "_MEIPASS", None)
    if isinstance(meipass, str) and meipass:
        meipass_dir = Path(meipass)
        _add(meipass_dir)
        _add(meipass_dir / "_internal")
        _add(meipass_dir / "_internal" / "cuda")
        _add(meipass_dir / "cuda")

    site_packages = (Path(sys.executable).parent / ".." / "Lib" / "site-packages").resolve()
    _add(site_packages / "nvidia")

    search_patterns = (
        "nvidia/*/bin",
        "nvidia/*/lib",
        "_internal/cuda",
        "_internal/nvidia/*/bin",
        "_internal/nvidia/*/lib",
        "nvidia/*/bin",
        "nvidia/*/lib",
    )

    expanded: list[Path] = []
    for base in candidate_dirs:
        for pattern in search_patterns:
            try:
                expanded.extend(base.glob(pattern))
            except OSError:
                continue

    registered_dirs: list[str] = []
    for path in expanded:
        if not path.is_dir():
            continue
        # Prefer directories that look like CUDA runtime folders.
        dll_names = {p.name.lower() for p in path.glob("*.dll")}
        if not any(
            name.startswith(("cublas", "cudnn", "cudart", "nvrtc", "cuda"))
            for name in dll_names
        ):
            continue
        dir_str = str(path)
        registered_dirs.append(dir_str)
        try:
            os.add_dll_directory(dir_str)
        except OSError:
            pass

    if registered_dirs:
        # Keep order stable while removing duplicates.
        ordered_unique = list(dict.fromkeys(registered_dirs))
        os.environ["PATH"] = os.pathsep.join(ordered_unique) + os.pathsep + os.environ.get("PATH", "")

_register_cuda_dll_dirs()


def _set_windows_app_user_model_id() -> None:
    """Give Windows a stable app identity for taskbar icon grouping."""
    if sys.platform != "win32":
        return

    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "PyroDS.TachyonTranscripts",
        )
    except Exception:
        # Non-critical: Tk/pystray can still run if Windows rejects this.
        pass


from tachyon.config import Config, LoopbackDevice
from tachyon.hotkey import HotkeyListener
from tachyon.capture import AudioCapture
from tachyon.transcriber import Transcriber
from tachyon.session import Session
from tachyon.exporter import (
    export_transcript,
    export_transcript_diarized,
    load_transcript_from_markdown,
    next_version_number,
)
from tachyon.batch import BatchTranscriber, BatchProgress
from tachyon.diarizer import (
    Diarizer,
    DiarizeConfig,
    DiarizeProgress,
    SpeakerInfo,
    save_speaker_map,
    update_speaker_names,
)
from tachyon.ui.tray import TrayIcon
from tachyon.ui.overlay import CaptionOverlay
from tachyon.ui.reviewer import TranscriptReviewer

logger = logging.getLogger(__name__)


def _friendly_model_load_error(exc: Exception) -> str:
    """Return user-facing guidance for common startup model-load failures."""
    msg = str(exc).lower()
    if "cublas64_12.dll" in msg:
        return (
            "Missing CUDA runtime (cublas64_12.dll). Reinstall Tachyon, check "
            "antivirus quarantine, or set compute_device to 'cpu' in config.json."
        )
    if "cudnn64_9.dll" in msg:
        return (
            "Missing CUDA runtime (cudnn64_9.dll). Reinstall Tachyon, check "
            "antivirus quarantine, or set compute_device to 'cpu' in config.json."
        )
    if "cuda" in msg and "driver" in msg:
        return (
            "NVIDIA driver may be missing or outdated for CUDA 12. Update the "
            "driver or set compute_device to 'cpu' in config.json."
        )
    if "dll" in msg and "cannot be loaded" in msg:
        return (
            "A required runtime DLL could not be loaded. Reinstall Tachyon and "
            "check antivirus quarantine."
        )
    return (
        "Check tachyon.log for details. If needed, set compute_device to 'cpu' "
        "in config.json as a fallback."
    )


def _discover_wav_files(audio_dir: Path) -> list[Path]:
    """Discover all WAV files in a session's audio directory.

    Reads ``device_manifest.json`` if present to get the full list.
    Falls back to checking for ``mic.wav`` and ``system.wav`` directly.

    Returns a list of existing WAV file paths.
    """
    manifest_path = audio_dir / "device_manifest.json"
    wav_files: list[Path] = []

    if manifest_path.exists():
        try:
            import json
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            # Mic
            mic_file = data.get("mic", {}).get("file")
            if mic_file:
                p = audio_dir / mic_file
                if p.exists():
                    wav_files.append(p)
            # Loopback(s)
            for lb in data.get("loopback", []):
                lb_file = lb.get("file")
                if lb_file:
                    p = audio_dir / lb_file
                    if p.exists():
                        wav_files.append(p)
            return wav_files
        except Exception:
            logger.warning("Failed to read device_manifest.json, falling back", exc_info=True)

    # Fallback: check for standard files
    for name in ("mic.wav", "system.wav"):
        p = audio_dir / name
        if p.exists():
            wav_files.append(p)

    return wav_files


class App:
    """Main application controller.

    Owns all component instances and manages their lifecycle through
    start/stop recording events triggered by the system tray menu.
    """

    def __init__(self) -> None:
        self._config = Config.load()
        logger.info("Config loaded: %s", self._config)

        # Shared queues
        self._audio_queue: queue.Queue = queue.Queue(maxsize=100)
        self._overlay_queue: queue.Queue = queue.Queue(maxsize=50)

        # Components (initialized once)
        self._capture: AudioCapture | None = None
        self._transcriber: Transcriber | None = None
        self._session: Session | None = None
        self._session_dir: Path | None = None

        # Recording state
        self._recording = False
        self._transcription_error_code: str = ""
        self._transcription_error_hint: str = ""

        # Batch re-transcription state
        self._batch_thread: Optional[threading.Thread] = None
        self._batch_stop_event: threading.Event = threading.Event()

        # Diarization state
        self._diarize_thread: Optional[threading.Thread] = None
        self._diarize_stop_event: threading.Event = threading.Event()
        self._diarize_session_dir: Optional[Path] = None
        self._diarize_source_transcript: Optional[str] = None
        self._diarize_backend: str = ""

        # Overlay
        self._overlay = CaptionOverlay(
            segment_queue=self._overlay_queue,
            position=self._config.overlay_position,
            opacity=self._config.overlay_opacity,
            on_close=self._on_overlay_closed,
        )
        self._captions_visible = True

        # Reviewer (created lazily on first open)
        self._reviewer: Optional[TranscriptReviewer] = None

        # Global hotkey listener (initialised in _start_tray_and_hotkey)
        self._hotkey_listener: Optional[HotkeyListener] = None

        # System tray
        self._tray = TrayIcon(
            on_start=self._on_start_recording,
            on_stop=self._on_stop_recording,
            on_toggle_overlay=self._on_toggle_overlay,
            on_set_output_folder=self._on_set_output_folder,
            on_open_output_folder=self._on_open_output_folder,
            on_set_mic_device=self._on_set_mic_device,
            on_set_loopback_devices=self._on_set_loopback_devices,
            on_review=self._on_review,
            on_quit=self._on_quit,
            on_show_wizard=self._on_show_wizard,
        )
        self._tray.set_mic_device(self._config.mic_device)
        # Initialize loopback device checkmarks in tray menu
        self._tray.set_loopback_devices(
            [d.get("device_name", "") for d in self._config.loopback_devices
             if d.get("enabled", True) and d.get("device_name")]
        )

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the application.

        Flow:
          1. Enter the tkinter mainloop (the overlay's Tk root).
          2. Via ``root.after(0, ...)``, either show the first-run wizard
             (if this is a new install) or go straight to startup.
          3. After the wizard (or immediately if skipped), bring up the
             tray + hotkey immediately and kick off the Whisper model
             load in a background thread.  The tray shows live status
             and lets the user Quit even while the model is loading.
        """
        needs_wizard = not self._config.first_run_complete
        if needs_wizard:
            logger.info("First run detected — scheduling setup wizard.")
            self._overlay._root.after(50, self._run_wizard_then_startup)
        else:
            self._overlay._root.after(50, self._post_wizard_startup)

        # Run the overlay mainloop (blocks until quit)
        self._overlay.run()

    # ------------------------------------------------------------------
    # Startup helpers — run inside the tkinter mainloop via root.after()
    # ------------------------------------------------------------------

    def _run_wizard_then_startup(self) -> None:
        """Show the first-run wizard modally, then continue to startup."""
        from tachyon.ui.wizard import FirstRunWizard
        from tachyon.hardware import detect_hardware

        # Detect hardware once so the wizard can show it and so the
        # transcriber doesn't re-detect later.
        hw = detect_hardware()
        self._detected_hw = hw

        # Enumerate devices for the wizard's picker pages.
        try:
            mic_devices = AudioCapture.get_devices()
        except Exception:
            logger.warning("Failed to enumerate mic devices for wizard", exc_info=True)
            mic_devices = []
        try:
            loopback_devices = AudioCapture.get_loopback_devices()
        except Exception:
            logger.warning("Failed to enumerate loopback devices for wizard", exc_info=True)
            loopback_devices = []

        wizard = FirstRunWizard(
            root=self._overlay._root,
            config=self._config,
            hardware_info=hw,
            mic_devices=mic_devices,
            loopback_devices=loopback_devices,
        )
        wizard.run()  # blocks (via wait_window) until wizard closes

        # Persist any changes made in the wizard.  Even if the user
        # closed it early, we save consent_acknowledged if they ticked
        # the box — so they don't have to do it twice.
        self._config.save()

        # Sync tray menu state with any device changes from the wizard
        self._tray.set_mic_device(self._config.mic_device)
        self._tray.set_loopback_devices(
            [d.get("device_name", "") for d in self._config.loopback_devices
             if d.get("enabled", True) and d.get("device_name")]
        )

        # Continue with the rest of startup
        self._post_wizard_startup()

    def _post_wizard_startup(self) -> None:
        """Tray-first startup: bring up the tray immediately, then load
        the transcription model in a background thread.

        Loading the Whisper model can take several minutes on first run
        (network download + CPU init).  Showing the tray right away gives
        the user visible feedback, a tooltip describing the current
        state, and an always-available Quit menu.
        """
        # Show the tray + tooltip *before* the model load kicks off.
        self._tray.set_model_ready(False)
        self._tray.set_status(
            "Loading transcription model… (first run downloads ~600 MB)"
        )
        self._start_tray_and_hotkey()

        # Hand the (slow) model load off to a daemon thread so the
        # tkinter mainloop and the tray remain responsive.
        threading.Thread(
            target=self._load_model_worker,
            name="ModelLoader",
            daemon=True,
        ).start()

    def _load_model_worker(self) -> None:
        """Background-load the Whisper model and update the tray when done."""
        logger.info("Loading transcription model (background thread)...")
        load_done = threading.Event()

        try:
            self._transcriber = Transcriber(
                chunk_queue=self._audio_queue,
                on_segment=self._on_segment,
                on_runtime_error=self._on_transcriber_runtime_error,
                model_size=self._config.model_size,
                device=self._config.compute_device,
            )
            # Heartbeat so the log shows forward motion even while the HF
            # download (which uses tqdm, not logging) is running.
            threading.Thread(
                target=self._model_load_heartbeat,
                args=(load_done,),
                name="ModelLoaderHeartbeat",
                daemon=True,
            ).start()
            self._transcriber.load_model()
        except Exception as exc:
            logger.exception("Failed to load Whisper model — cannot continue.")
            self._tray.set_status("Model failed to load — see tachyon.log")
            hint = _friendly_model_load_error(exc)
            self._tray.notify(
                "Tachyon Transcripts",
                "Failed to load the transcription model. "
                f"{hint}",
            )
            return
        finally:
            load_done.set()

        logger.info(
            "Model loaded: %s on %s (%s).",
            self._transcriber.resolved_model_size,
            self._transcriber.device,
            self._transcriber.compute_type,
        )

        self._tray.set_status(None)
        self._tray.set_model_ready(True)

        if self._transcriber.fell_back_to_cpu:
            self._tray.notify(
                "Tachyon Transcripts",
                f"GPU unavailable — running on CPU with the "
                f"'{self._transcriber.resolved_model_size}' model. "
                "Transcription will be slower.",
            )
        else:
            self._tray.notify(
                "Tachyon Transcripts",
                "Ready. Right-click the tray icon to start recording.",
            )

    def _model_load_heartbeat(self, done: threading.Event) -> None:
        """Log a 'still loading' line every 30s until ``done`` is set.

        Hugging Face's downloader reports progress via tqdm, which never
        reaches our log file.  Without this, the log goes silent for
        minutes during the first-run download and operators assume the
        app has hung.
        """
        import time
        start = time.monotonic()
        while not done.wait(timeout=30.0):
            elapsed = int(time.monotonic() - start)
            logger.info("Model load still in progress (%ds elapsed)...", elapsed)

    def _show_wizard(self) -> None:
        """Show the first-run wizard on-demand (from tray menu or consent gate).

        Runs on the tkinter main thread.  Unlike the startup flow, does
        NOT re-load the model or re-init the tray — just walks the user
        through the setup pages and saves their choices.
        """
        from tachyon.ui.wizard import FirstRunWizard
        from tachyon.hardware import detect_hardware

        hw = detect_hardware()
        try:
            mic_devices = AudioCapture.get_devices()
        except Exception:
            logger.warning("Failed to enumerate mic devices for wizard", exc_info=True)
            mic_devices = []
        try:
            loopback_devices = AudioCapture.get_loopback_devices()
        except Exception:
            logger.warning("Failed to enumerate loopback devices for wizard", exc_info=True)
            loopback_devices = []

        wizard = FirstRunWizard(
            root=self._overlay._root,
            config=self._config,
            hardware_info=hw,
            mic_devices=mic_devices,
            loopback_devices=loopback_devices,
        )
        wizard.run()

        # Persist whatever changes the user made and sync tray menu
        self._config.save()
        self._tray.set_mic_device(self._config.mic_device)
        self._tray.set_loopback_devices(
            [d.get("device_name", "") for d in self._config.loopback_devices
             if d.get("enabled", True) and d.get("device_name")]
        )

    def _on_show_wizard(self) -> None:
        """Tray-thread entry point for showing the wizard.

        Schedules the wizard on the tkinter main thread since tkinter
        widgets must only be touched from the UI thread.
        """
        self._overlay._root.after(0, self._show_wizard)

    def _start_tray_and_hotkey(self) -> None:
        """Spin up the system tray and register the global hotkey."""
        # Start system tray in a daemon thread
        tray_thread = threading.Thread(target=self._tray.run, daemon=True)
        tray_thread.start()

        # Register global hotkey for overlay toggle via native Win32
        # RegisterHotKey -- avoids the global keyboard hook from the
        # `keyboard` library that AV products tend to flag.
        try:
            listener = HotkeyListener(
                self._config.hotkey, self._on_toggle_overlay,
            )
            listener.start()
            self._hotkey_listener = listener
        except Exception:
            logger.warning(
                "Failed to register hotkey '%s'", self._config.hotkey,
                exc_info=True,
            )
            self._hotkey_listener = None

    # ------------------------------------------------------------------
    # Recording lifecycle
    # ------------------------------------------------------------------

    def _on_start_recording(self) -> None:
        """Called when user clicks 'Start Recording' in the tray menu."""
        if self._recording:
            logger.warning("Already recording — ignoring start request")
            return

        # Consent gate — block recording until the user has acknowledged
        # the recording-law disclaimer at least once.
        if not self._config.consent_acknowledged:
            logger.warning(
                "Recording blocked: consent disclaimer not acknowledged.",
            )
            self._tray.notify(
                "Tachyon Transcripts",
                "Please complete the setup wizard before recording. "
                "You need to acknowledge the recording-law disclaimer.",
            )
            self._overlay._root.after(0, self._show_wizard)
            return

        if self._transcriber is None or not self._transcriber.model_loaded:
            logger.warning("Cannot start recording — transcription model not loaded")
            self._tray.notify(
                "Tachyon Transcripts",
                "Transcription model failed to load — recording unavailable. "
                "Check tachyon.log for details.",
            )
            return

        if self._batch_thread is not None and self._batch_thread.is_alive():
            logger.warning("Cannot start recording while batch re-transcription is running")
            self._tray.notify(
                "Tachyon Transcripts",
                "Cannot record while re-transcription is running.",
            )
            return

        if self._diarize_thread is not None and self._diarize_thread.is_alive():
            logger.warning("Cannot start recording while diarization is running")
            self._tray.notify(
                "Tachyon Transcripts",
                "Cannot record while speaker identification is running.",
            )
            return

        logger.info("Starting recording...")
        self._transcription_error_code = ""
        self._transcription_error_hint = ""
        self._tray.set_status(None)

        # Create a new session (not yet "recording" — only after capture starts)
        self._session = Session()
        self._session.start()

        # Set session start time on transcriber for relative timestamps
        self._transcriber.set_session_start_time(self._session.start_time)

        # Prepare output directory
        output_dir = self._config.get_output_path()
        folder_name = self._session.start_datetime.strftime("%Y-%m-%d_%H%M%S")
        self._session_dir = output_dir / folder_name
        audio_dir = self._session_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)

        # Build loopback configs from config
        loopback_configs = self._config.get_active_loopback_devices()

        # Start audio capture — guarded so a device failure doesn't leave the
        # app in a half-recording state
        self._capture = AudioCapture(
            chunk_queue=self._audio_queue,
            mic_device=self._config.mic_device,
            output_device=self._config.output_device,
            loopback_configs=loopback_configs if loopback_configs else None,
        )
        try:
            self._capture.start(audio_dir)
        except Exception:
            logger.exception("Failed to start audio capture")
            try:
                self._capture.stop()
            except Exception:
                logger.warning("Error while tearing down failed capture", exc_info=True)
            self._capture = None
            self._session = None
            self._session_dir = None
            self._tray.notify(
                "Tachyon Transcripts",
                "Recording failed to start — check mic/audio device settings.",
            )
            return

        # Commit recording state only after capture is running
        self._recording = True
        self._tray.set_recording(True)
        self._overlay.clear_history()
        self._overlay.set_recording(True)

        if not self._capture.mic_active:
            self._tray.notify(
                "Tachyon Transcripts",
                "Microphone unavailable — recording system audio only.",
            )
        if not self._capture.loopback_active:
            self._tray.notify(
                "Tachyon Transcripts",
                "System audio capture unavailable — recording mic only.",
            )

        # Start transcriber worker
        self._transcriber.start()

        # Notify reviewer that recording is active
        if self._reviewer is not None:
            self._reviewer.set_recording_active(True)

        self._tray.notify("Tachyon Transcripts", "Recording started.")
        logger.info("Recording started — output: %s", self._session_dir)

    def _on_stop_recording(self) -> None:
        """Called when user clicks 'Stop Recording' in the tray menu."""
        if not self._recording:
            logger.warning("Not recording — ignoring stop request")
            return

        logger.info("Stopping recording...")
        self._recording = False
        self._tray.set_recording(False)
        self._overlay.set_recording(False)

        # Stop capture and transcription
        if self._capture is not None:
            self._capture.stop()

        if self._transcriber is not None:
            self._transcriber.stop(drain=True)

        # Export transcript
        if self._session is not None and self._session_dir is not None:
            try:
                export_transcript(self._session, self._config.get_output_path())
                logger.info("Transcript exported to %s", self._session_dir)
                self._tray.set_last_session_time(self._session.start_datetime)
                if self._transcription_error_code:
                    if self._session.segment_count == 0:
                        self._tray.notify(
                            "Tachyon Transcripts",
                            "Audio saved, but transcript failed. "
                            f"{self._transcription_error_hint}",
                        )
                    else:
                        self._tray.notify(
                            "Tachyon Transcripts",
                            "Recording saved with a partial transcript. "
                            f"{self._transcription_error_hint}",
                        )
                else:
                    self._tray.notify(
                        "Tachyon Transcripts",
                        f"Recording saved to {self._session_dir.name}",
                    )
            except Exception:
                logger.exception("Failed to export transcript")
                self._tray.notify(
                    "Tachyon Transcripts",
                    "Error exporting transcript — check logs.",
                )

        self._session = None
        self._session_dir = None

        # Notify reviewer that recording is no longer active
        if self._reviewer is not None:
            self._reviewer.set_recording_active(False)
            self._reviewer.refresh()

        self._tray.set_status(None)
        logger.info("Recording stopped.")

    # ------------------------------------------------------------------
    # Segment callback (called from transcriber thread)
    # ------------------------------------------------------------------

    def _on_segment(self, segment) -> None:
        """Called by the transcriber when a new segment is ready."""
        # Add to session log
        if self._session is not None:
            self._session.add_segment(segment)

        # Send to overlay display queue
        try:
            self._overlay_queue.put_nowait(segment)
        except queue.Full:
            pass  # overlay will catch up

    def _on_transcriber_runtime_error(self, error_code: str, user_hint: str) -> None:
        """Handle fatal runtime inference errors from transcriber thread."""
        logger.error("Transcriber runtime error (%s): %s", error_code, user_hint)
        self._overlay._root.after(
            0,
            self._handle_transcriber_runtime_error,
            error_code,
            user_hint,
        )

    def _handle_transcriber_runtime_error(
        self,
        error_code: str,
        user_hint: str,
    ) -> None:
        """Main-thread handler for transcriber runtime failures."""
        if self._transcription_error_code:
            return
        self._transcription_error_code = error_code
        self._transcription_error_hint = user_hint
        self._tray.set_status("Transcription failed — audio capture continues")
        self._tray.notify(
            "Tachyon Transcripts",
            "Transcription failed during recording. Audio is still being saved. "
            f"{user_hint}",
        )

    # ------------------------------------------------------------------
    # UI callbacks
    # ------------------------------------------------------------------

    def _on_toggle_overlay(self) -> None:
        """Toggle caption overlay visibility."""
        self._captions_visible = not self._captions_visible
        self._overlay.toggle()
        self._tray.set_captions_visible(self._captions_visible)

    def _on_overlay_closed(self) -> None:
        """Called when the user clicks the overlay close button."""
        self._captions_visible = False
        self._tray.set_captions_visible(False)

    def _on_set_output_folder(self) -> None:
        """Tray-thread entry point for "Set Output Folder..." click.

        The tkinter folder-picker must be shown from the main UI thread,
        not the pystray worker thread.  Schedule the actual dialog on
        the root's event loop.
        """
        self._overlay._root.after(0, self._pick_output_folder)

    def _pick_output_folder(self) -> None:
        """Show the folder-picker dialog on the main tkinter thread.

        Must only be called from the tkinter main thread (see
        :meth:`_on_set_output_folder`).
        """
        from tkinter import filedialog

        try:
            folder = filedialog.askdirectory(
                parent=self._overlay._root,
                title="Select Output Folder",
            )
        except Exception:
            logger.exception("Failed to open output-folder dialog")
            return

        if not folder:
            logger.debug("Output folder selection cancelled")
            return

        path = Path(folder)
        self._config.output_dir = str(path)
        self._config.save()
        logger.info("Output folder changed to %s", path)

    def _on_set_mic_device(self, device_name: str | None) -> None:
        """Update the configured microphone device.

        Takes effect on the next recording start (no hot-swap mid-session).
        """
        self._config.mic_device = device_name
        self._config.save()
        logger.info("Microphone set to %s", device_name or "System Default")

    def _on_set_loopback_devices(self, device_names: list[str]) -> None:
        """Update configured loopback devices.

        Takes effect on the next recording start (no hot-swap mid-session).

        Parameters
        ----------
        device_names:
            List of device name strings.  Empty = system default.
        """
        if not device_names:
            # Reset to system default (empty list)
            self._config.loopback_devices = []
        else:
            self._config.loopback_devices = [
                {"device_name": name, "label": self._extract_label(name), "enabled": True}
                for name in device_names
            ]
        self._config.save()
        logger.info("Loopback devices set to %s", device_names or ["System Default"])

    @staticmethod
    def _extract_label(device_name: str) -> str:
        """Extract a short label from a loopback device name.

        Tries to find a recognizable short name from the device string.
        E.g. "Headset Earphone (Arctis 7 Chat)" -> "Chat"
             "Headphones (Arctis 7 Game)" -> "Game"
        Falls back to the first word in parentheses or the device name itself.
        """
        import re
        # Look for text in parentheses
        m = re.search(r"\((.+?)\)", device_name)
        if m:
            inner = m.group(1).strip()
            # Take the last word (often the distinguishing part)
            parts = inner.split()
            if parts:
                return parts[-1]
        # Fallback: first significant word
        parts = device_name.split()
        return parts[0] if parts else device_name

    def _on_open_output_folder(self) -> None:
        """Open the output folder in Windows Explorer."""
        output_path = self._config.get_output_path()
        output_path.mkdir(parents=True, exist_ok=True)
        os.startfile(output_path)
        logger.info("Opened output folder: %s", output_path)

    # ------------------------------------------------------------------
    # Review & batch re-transcription
    # ------------------------------------------------------------------

    def _on_review(self) -> None:
        """Open the transcript review window (from tray, runs on tray thread)."""
        # Schedule on main thread since tkinter must be touched there
        self._overlay._root.after(0, self._show_reviewer)

    def _show_reviewer(self) -> None:
        """Create/show the reviewer on the main tkinter thread."""
        if self._reviewer is None:
            self._reviewer = TranscriptReviewer(
                root=self._overlay._root,
                output_dir=self._config.get_output_path(),
                on_retranscribe=self._on_retranscribe,
                on_cancel_retranscribe=self._on_cancel_retranscribe,
                on_diarize=self._on_diarize,
                on_cancel_diarize=self._on_cancel_diarize,
                on_save_speaker_names=self._on_save_speaker_names,
                on_hf_token_changed=self._on_hf_token_changed,
                on_save_geometry=self._on_save_reviewer_geometry,
                tutorial_show_on_open=self._config.reviewer_tutorial_show_on_open,
                on_tutorial_preference_changed=self._on_save_reviewer_tutorial_preference,
            )
            self._reviewer.set_backend_config(
                self._config.diarize_backend, self._config.hf_token,
            )
            self._reviewer.set_initial_geometry(self._config.reviewer_geometry)
        self._reviewer.set_recording_active(self._recording)
        self._reviewer.show()

    def _on_retranscribe(self, session_dir: Path) -> None:
        """Start batch re-transcription in a daemon thread."""
        if self._recording:
            logger.warning("Cannot re-transcribe while recording")
            return
        if self._batch_thread is not None and self._batch_thread.is_alive():
            logger.warning("Batch re-transcription already running")
            return
        if self._diarize_thread is not None and self._diarize_thread.is_alive():
            logger.warning("Cannot re-transcribe while diarization is running")
            return

        # Model must be loaded — BatchTranscriber requires it and raises
        # ValueError on None, which would crash the batch thread silently.
        if self._transcriber is None or self._transcriber.model is None:
            logger.warning(
                "Cannot re-transcribe: transcription model not loaded.",
            )
            self._tray.notify(
                "Tachyon Transcripts",
                "Re-transcription unavailable — model failed to load. "
                "Check tachyon.log for details.",
            )
            if self._reviewer is not None:
                self._reviewer.set_batch_running(False)
            return

        logger.info("Starting batch re-transcription for %s", session_dir)

        self._batch_stop_event.clear()
        self._tray.set_batch_running(True)
        if self._reviewer is not None:
            self._reviewer.set_batch_running(True)

        batch = BatchTranscriber(
            model=self._transcriber.model,
            on_progress=self._on_batch_progress,
        )

        self._batch_thread = threading.Thread(
            target=self._batch_worker,
            args=(batch, session_dir),
            name="BatchTranscriber",
            daemon=True,
        )
        self._batch_thread.start()

    def _batch_worker(self, batch: BatchTranscriber, session_dir: Path) -> None:
        """Run batch re-transcription (on worker thread)."""
        try:
            result_path = batch.transcribe_and_export(
                session_dir, self._batch_stop_event,
            )

            if result_path is not None:
                logger.info("Batch transcription saved to %s", result_path)
                self._overlay._root.after(0, self._on_batch_complete, result_path)
            else:
                cancelled = self._batch_stop_event.is_set()
                if cancelled:
                    logger.info("Batch transcription cancelled by user")
                else:
                    logger.warning("Batch transcription produced no segments")
                self._overlay._root.after(0, self._on_batch_finished)
                if not cancelled:
                    self._overlay._root.after(0, lambda: self._tray.notify(
                        "Tachyon Transcripts",
                        "Re-transcription produced no results \u2014 audio may be silent or corrupt.",
                    ))
                    if self._reviewer is not None:
                        self._overlay._root.after(
                            0,
                            self._reviewer.show_error,
                            "Re-transcription produced no results. "
                            "The audio files may be silent or corrupt.",
                        )
        except Exception as exc:
            logger.exception("Batch re-transcription failed")
            self._overlay._root.after(0, self._on_batch_finished)
            self._overlay._root.after(0, lambda: self._tray.notify(
                "Tachyon Transcripts",
                "Re-transcription failed \u2014 check logs.",
            ))
            if self._reviewer is not None:
                msg = f"Re-transcription failed: {exc}"
                self._overlay._root.after(
                    0, self._reviewer.show_error, msg,
                )

    def _on_batch_progress(self, progress: BatchProgress) -> None:
        """Forward batch progress to reviewer (from worker thread)."""
        if self._reviewer is not None:
            self._overlay._root.after(
                0, self._reviewer.update_progress, progress,
            )

    def _on_batch_complete(self, result_path: Path) -> None:
        """Handle successful batch completion (on main thread)."""
        self._on_batch_finished()
        if self._reviewer is not None:
            self._reviewer.on_retranscribe_complete()
        self._tray.notify(
            "Tachyon Transcripts",
            f"Re-transcription complete: {result_path.name}",
        )

    def _on_batch_finished(self) -> None:
        """Clean up batch state (on main thread)."""
        self._tray.set_batch_running(False)
        if self._reviewer is not None:
            self._reviewer.set_batch_running(False)

    def _on_cancel_retranscribe(self) -> None:
        """Cancel a running batch re-transcription."""
        logger.info("Cancelling batch re-transcription")
        self._batch_stop_event.set()

    # ------------------------------------------------------------------
    # Speaker diarization
    # ------------------------------------------------------------------

    def _on_diarize(
        self, session_dir: Path, source_transcript: str,
        backend: str, hf_token: str, num_speakers: Optional[int],
    ) -> None:
        """Start speaker diarization in a daemon thread."""
        if self._recording:
            logger.warning("Cannot diarize while recording")
            return
        if self._batch_thread is not None and self._batch_thread.is_alive():
            logger.warning("Cannot diarize while batch re-transcription is running")
            return
        if self._diarize_thread is not None and self._diarize_thread.is_alive():
            logger.warning("Diarization already running")
            return

        # Persist backend + token choice to config
        self._config.diarize_backend = backend
        if hf_token:
            self._config.hf_token = hf_token
        self._config.save()

        logger.info(
            "Starting diarization for %s (source: %s, backend: %s)",
            session_dir, source_transcript, backend,
        )

        self._diarize_stop_event.clear()
        self._diarize_session_dir = session_dir
        self._diarize_source_transcript = source_transcript
        self._diarize_backend = backend

        self._tray.set_batch_running(True)  # reuse batch state for mutual exclusion
        if self._reviewer is not None:
            self._reviewer.set_diarize_running(True)

        config = DiarizeConfig(
            backend=backend,
            hf_token=hf_token,
            num_speakers=num_speakers,
        )
        diarizer = Diarizer(config=config, on_progress=self._on_diarize_progress)

        self._diarize_thread = threading.Thread(
            target=self._diarize_worker,
            args=(diarizer, session_dir, source_transcript),
            name="Diarizer",
            daemon=True,
        )
        self._diarize_thread.start()

    def _diarize_worker(
        self,
        diarizer: Diarizer,
        session_dir: Path,
        source_transcript: str,
    ) -> None:
        """Run diarization (on worker thread)."""
        try:
            result = diarizer.diarize_session(
                session_dir, source_transcript, self._diarize_stop_event,
            )

            if result is not None:
                segments, speaker_infos = result

                # Export diarized transcript
                import soundfile as _sf
                duration = 0.0
                for wav_path in _discover_wav_files(session_dir / "audio"):
                    if wav_path.exists():
                        info = _sf.info(str(wav_path))
                        duration = max(duration, info.duration)

                from tachyon.batch import BatchTranscriber
                start_dt = BatchTranscriber._parse_session_datetime(session_dir)
                version = next_version_number(session_dir)

                export_transcript_diarized(
                    segments, session_dir, duration, start_dt, version,
                    backend=self._diarize_backend,
                )

                # Save speaker map
                speaker_map = {
                    info.id: info.display_name for info in speaker_infos
                }
                save_speaker_map(
                    session_dir, speaker_map,
                    len(speaker_infos), source_transcript,
                )

                logger.info("Diarization complete: %d speakers", len(speaker_infos))
                self._overlay._root.after(
                    0, self._on_diarize_complete, speaker_infos,
                )
            else:
                cancelled = self._diarize_stop_event.is_set()
                if cancelled:
                    logger.info("Diarization cancelled by user")
                else:
                    logger.warning("Diarization produced no result")
                self._overlay._root.after(0, self._on_diarize_finished)
                if not cancelled:
                    self._overlay._root.after(0, lambda: self._tray.notify(
                        "Tachyon Transcripts",
                        "Speaker identification failed \u2014 check logs for details.",
                    ))
                    if self._reviewer is not None:
                        self._overlay._root.after(
                            0,
                            self._reviewer.show_error,
                            "Speaker identification failed. The loopback "
                            "audio may be missing, silent, or too short.",
                        )
        except Exception as exc:
            logger.exception("Diarization failed")
            self._overlay._root.after(0, self._on_diarize_finished)
            self._overlay._root.after(0, lambda: self._tray.notify(
                "Tachyon Transcripts",
                "Speaker identification failed \u2014 check logs.",
            ))
            if self._reviewer is not None:
                msg = f"Speaker identification failed: {exc}"
                self._overlay._root.after(
                    0, self._reviewer.show_error, msg,
                )

    def _on_diarize_progress(self, progress: DiarizeProgress) -> None:
        """Forward diarize progress to reviewer (from worker thread)."""
        if self._reviewer is not None:
            self._overlay._root.after(
                0, self._reviewer.update_diarize_progress, progress,
            )

    def _on_diarize_complete(self, speaker_infos: list[SpeakerInfo]) -> None:
        """Handle successful diarization (on main thread)."""
        self._on_diarize_finished()
        if self._reviewer is not None:
            self._reviewer.on_diarize_complete(speaker_infos)
        self._tray.notify(
            "Tachyon Transcripts",
            f"Speaker identification complete: {len(speaker_infos)} speakers detected.",
        )

    def _on_diarize_finished(self) -> None:
        """Clean up diarize state (on main thread)."""
        self._tray.set_batch_running(False)
        if self._reviewer is not None:
            self._reviewer.set_diarize_running(False)

    def _on_cancel_diarize(self) -> None:
        """Cancel a running diarization."""
        logger.info("Cancelling diarization")
        self._diarize_stop_event.set()

    def _on_hf_token_changed(self, token: str) -> None:
        """Persist HuggingFace token change from the reviewer UI."""
        self._config.hf_token = token
        self._config.save()
        logger.info("HF token %s", "updated" if token else "deleted")

    def _on_save_reviewer_geometry(self, geometry: str) -> None:
        """Persist reviewer window geometry to config."""
        self._config.reviewer_geometry = geometry
        self._config.save()
        logger.debug("Reviewer geometry saved: %s", geometry)

    def _on_save_reviewer_tutorial_preference(self, show_on_open: bool) -> None:
        """Persist reviewer tutorial auto-show preference to config."""
        self._config.reviewer_tutorial_show_on_open = show_on_open
        self._config.save()
        logger.debug("Reviewer tutorial auto-show saved: %s", show_on_open)

    def _on_save_speaker_names(
        self, session_dir: Path, names: dict[str, str],
    ) -> None:
        """Save user-assigned speaker names and re-export the transcript.

        Called from the inline speaker panel after the user clicks Save Names.
        """
        logger.info("Saving speaker names: %s", names)

        # Update speaker_map.json
        update_speaker_names(session_dir, names)

        # Re-export the diarized transcript with updated names
        import json
        map_path = session_dir / "speaker_map.json"
        if not map_path.exists():
            return

        data = json.loads(map_path.read_text(encoding="utf-8"))
        source_file = data.get("source_transcript")
        if not source_file:
            return

        # Find the most recent diarized transcript and update it
        from tachyon.exporter import discover_versions
        versions = discover_versions(session_dir)
        if not versions:
            return

        latest_diarized = None
        saved_backend = ""
        for v in reversed(versions):
            path = session_dir / v
            if path.exists():
                try:
                    head = path.read_text(encoding="utf-8")[:500]
                    if "Diarized" in head:
                        latest_diarized = v
                        # Extract backend name from header e.g. "(Diarized — pyannote)"
                        import re as _re2
                        bm = _re2.search(r"Diarized\s*\u2014\s*(\w+)", head)
                        if bm:
                            saved_backend = bm.group(1)
                        break
                except Exception:
                    pass

        if latest_diarized is None:
            return

        # Load the diarized transcript
        transcript_path = session_dir / latest_diarized
        try:
            _, segments = load_transcript_from_markdown(transcript_path)
        except Exception:
            logger.exception("Failed to load diarized transcript for renaming")
            return

        # Apply name substitutions to segments
        from tachyon.session import TranscriptSegment
        from tachyon.diarizer import _parse_speaker_number

        updated_segments: list[TranscriptSegment] = []
        for seg in segments:
            new_speaker = seg.speaker
            if seg.speaker != "You" and seg.speaker.startswith("Speaker"):
                num = _parse_speaker_number(seg.speaker)
                speaker_id = f"speaker_{num}"
                if speaker_id in names and names[speaker_id].strip():
                    new_speaker = names[speaker_id]

            updated_segments.append(TranscriptSegment(
                speaker=new_speaker,
                text=seg.text,
                start_time=seg.start_time,
                end_time=seg.end_time,
            ))

        # Overwrite the diarized transcript with updated names
        import soundfile as _sf
        duration = 0.0
        for wav_path in _discover_wav_files(session_dir / "audio"):
            if wav_path.exists():
                info = _sf.info(str(wav_path))
                duration = max(duration, info.duration)

        from tachyon.batch import BatchTranscriber
        start_dt = BatchTranscriber._parse_session_datetime(session_dir)

        import re as _re
        m = _re.search(r"_v(\d+)\.md$", latest_diarized)
        version = int(m.group(1)) if m else next_version_number(session_dir)

        export_transcript_diarized(
            updated_segments, session_dir, duration, start_dt, version,
            speaker_names=names,
            backend=saved_backend,
        )

        logger.info("Re-exported diarized transcript with speaker names")

        # Refresh the reviewer via public method
        if self._reviewer is not None:
            self._reviewer.refresh_current_version()

        self._tray.notify(
            "Tachyon Transcripts",
            "Speaker names saved.",
        )

    def _on_quit(self) -> None:
        """Clean shutdown."""
        logger.info("Quit requested")

        # Stop recording if active
        if self._recording:
            self._on_stop_recording()

        # Cancel batch re-transcription if running
        if self._batch_thread is not None and self._batch_thread.is_alive():
            self._batch_stop_event.set()
            self._batch_thread.join(timeout=5.0)
            if self._batch_thread is not None and self._batch_thread.is_alive():
                logger.warning("Batch thread did not exit within timeout")

        # Cancel diarization if running
        if self._diarize_thread is not None and self._diarize_thread.is_alive():
            self._diarize_stop_event.set()
            self._diarize_thread.join(timeout=5.0)
            if self._diarize_thread is not None and self._diarize_thread.is_alive():
                logger.warning("Diarize thread did not exit within timeout")

        # Tear down components
        if self._hotkey_listener is not None:
            try:
                self._hotkey_listener.stop()
            except Exception:
                logger.warning("Error stopping hotkey listener", exc_info=True)
            self._hotkey_listener = None
        self._tray.stop()
        self._overlay.destroy()

        logger.info("Goodbye.")


def _download_model_cli() -> int:
    """Pre-download the Whisper model for the detected hardware and exit.

    Invoked by the installer's post-install ``[Run]`` step so first
    launch isn't blocked on a multi-minute download.  Writes progress to
    stdout (faster-whisper does this itself via huggingface_hub).

    Returns a process exit code — 0 on success, non-zero on failure.
    The installer does not block on failure, so a missing model only
    means first launch will be slower.
    """
    try:
        from tachyon.hardware import resolve_transcriber_config
        from faster_whisper import WhisperModel
    except Exception as exc:
        print(f"[download-model] import failed: {exc}", file=sys.stderr)
        return 2

    try:
        device, model_size, compute_type, hw = resolve_transcriber_config(
            requested_device="auto", requested_model_size="auto",
        )
        print(
            f"[download-model] detected: {hw.summary}\n"
            f"[download-model] downloading '{model_size}' for "
            f"{device} ({compute_type})...",
            flush=True,
        )
        # Instantiating WhisperModel triggers the download if the model
        # isn't already in the HF cache.  We just let it go out of scope.
        WhisperModel(model_size, device=device, compute_type=compute_type)
        print("[download-model] done.", flush=True)
        return 0
    except Exception as exc:
        print(f"[download-model] failed: {exc}", file=sys.stderr)
        return 1


def main() -> None:
    """Entry point."""
    # -----------------------------------------------------------------
    # CLI flags handled before heavy initialization
    # -----------------------------------------------------------------
    argv = sys.argv[1:]

    if "--version" in argv:
        # Keep in sync with installer/Tachyon.iss ``MyAppVersion``.
        print("Tachyon Transcripts 0.1.2")
        return

    if "--download-model" in argv:
        raise SystemExit(_download_model_cli())

    # Must be set before the first Tk window is created, otherwise Windows
    # groups the process under python.exe and may show Python's taskbar icon.
    _set_windows_app_user_model_id()

    from tachyon.config import PROJECT_ROOT

    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    log_datefmt = "%H:%M:%S"

    # Console handler (visible when run via python, silent via pythonw)
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        datefmt=log_datefmt,
    )

    # File handler — always writes to tachyon.log in the project root
    log_path = PROJECT_ROOT / "tachyon.log"
    file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.getLogger().addHandler(file_handler)

    logger.info("Tachyon Transcripts starting...")
    logger.info("Log file: %s", log_path)
    app = App()
    app.run()


if __name__ == "__main__":
    main()

"""Transcript review window for browsing past sessions and re-transcribing.

Provides a tkinter Toplevel window (parented to the overlay's tk.Tk root)
with:
  - Top toolbar: action buttons, status/progress, config controls
  - Left panel: enriched session list with duration, version count, indicators
  - Right panel: transcript viewer with speaker-colored text and timestamps
  - Inline speaker panel (between header and transcript) after diarization

Session discovery scans the output directory for folders matching the
``YYYY-MM-DD_HHMMSS`` naming pattern and checks for audio files and
transcript versions.

Threading
---------
Must be created and operated on the tkinter main thread.  Progress updates
from the batch transcriber arrive via ``update_progress()`` which should be
called through ``root.after(0, ...)`` from the batch worker thread.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from tachyon.exporter import (
    discover_versions,
    load_transcript_from_markdown,
    save_edited_segments,
)
from tachyon.session import TranscriptSegment
from tachyon.batch import BatchProgress
from tachyon.diarizer import DiarizeProgress, SpeakerInfo, SPEAKER_COLORS, load_speaker_map
from tachyon.ui.theme import Color, Font, Dim, ToolTip
from tachyon.ui.widgets import HoverButton, GradientBar, SessionCard

logger = logging.getLogger(__name__)

# Regex for session folder names
_SESSION_DIR_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})(\d{2})$")

_REVIEWER_TUTORIAL_STEPS: list[tuple[str, str]] = [
    (
        "Review your transcripts",
        "This screen helps you find recordings, read transcripts, clean them up, "
        "name who was speaking, and open the saved files.",
    ),
    (
        "Your recordings",
        "Pick a past recording from the left. Newest recordings are shown first, with quick "
        "hints like length and whether extra versions are available.",
    ),
    (
        "Search",
        "Type in Search to quickly narrow recordings by date or label.",
    ),
    (
        "Transcript view",
        "The right side shows timestamped transcript lines so you can quickly review what was said.",
    ),
    (
        "Transcript versions",
        "Use this dropdown to compare the original transcript with cleaned-up, speaker-labeled, "
        "or edited versions.",
    ),
    (
        "Clean up a transcript",
        "Use Re-transcribe when the first pass missed words. Tachyon listens again to the saved "
        "recording and creates a new improved version.",
    ),
    (
        "Name who was speaking",
        "Use Identify Speakers when several people are talking. Tachyon can separate voices so "
        "you can rename Speaker 1, Speaker 2, and so on.",
    ),
    (
        "Edit and save",
        "Use Edit for quick fixes. Saving creates a new version so your earlier transcript stays safe.",
    ),
    (
        "Find the files",
        "Open Folder shows the saved transcript and recording files for the selected session.",
    ),
]
_BACKEND_OPTIONS = ("speechbrain", "pyannote", "resemblyzer")


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------

@dataclass
class SessionInfo:
    """Metadata about a discovered past recording session.

    Attributes:
        path: Absolute path to the session folder.
        start_datetime: Parsed datetime from the folder name.
        display_label: Short label for the session list (e.g. "03-16 13:58").
        has_mic_wav: Whether audio/mic.wav exists.
        loopback_files: List of loopback WAV info dicts from device_manifest.json.
            Each dict has ``file``, ``label``, ``device``.  For old sessions
            without a manifest, a single ``{"file": "system.wav"}`` entry is
            used when ``system.wav`` exists.
        transcript_versions: List of transcript filenames found.
        duration_sec: Duration in seconds (from JSON sidecar), or None.
        is_diarized: Whether any version has been diarized.
    """

    path: Path
    start_datetime: datetime
    display_label: str
    has_mic_wav: bool
    loopback_files: list[dict] = field(default_factory=list)
    transcript_versions: list[str] = field(default_factory=list)
    duration_sec: Optional[float] = None
    is_diarized: bool = False


def discover_sessions(output_dir: Path) -> list[SessionInfo]:
    """Scan the output directory for past recording sessions.

    Returns a list of :class:`SessionInfo` sorted newest-first.

    Parameters
    ----------
    output_dir:
        Root output directory to scan (e.g. ``./output``).

    Returns
    -------
    list[SessionInfo]
        Discovered sessions, newest first.
    """
    sessions: list[SessionInfo] = []

    if not output_dir.exists():
        return sessions

    for entry in output_dir.iterdir():
        if not entry.is_dir():
            continue

        m = _SESSION_DIR_RE.match(entry.name)
        if not m:
            continue

        date_str, hour, minute, second = m.group(1), m.group(2), m.group(3), m.group(4)
        try:
            dt = datetime.strptime(entry.name, "%Y-%m-%d_%H%M%S")
        except ValueError:
            continue

        audio_dir = entry / "audio"
        has_mic = (audio_dir / "mic.wav").exists()
        versions = discover_versions(entry)

        # Discover loopback files from manifest or fallback
        loopback_files: list[dict] = []
        manifest_path = audio_dir / "device_manifest.json"
        if manifest_path.exists():
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                loopback_files = data.get("loopback", [])
            except Exception:
                logger.warning("Failed to read device manifest in %s", entry.name, exc_info=True)

        # Fallback for old sessions without manifest
        if not loopback_files and (audio_dir / "system.wav").exists():
            loopback_files = [{"file": "system.wav"}]

        label = dt.strftime("%m-%d %H:%M")

        # Peek at JSON sidecar for duration (lightweight — only read first sidecar)
        duration_sec: Optional[float] = None
        is_diarized = False
        if versions:
            # Check latest version first for duration
            for v in reversed(versions):
                sidecar = entry / v.replace(".md", ".json")
                if sidecar.exists():
                    try:
                        sdata = json.loads(sidecar.read_text(encoding="utf-8"))
                        if duration_sec is None and "duration_sec" in sdata:
                            duration_sec = sdata["duration_sec"]
                        if sdata.get("source") == "diarized":
                            is_diarized = True
                    except Exception:
                        pass
                    if duration_sec is not None:
                        break

            # Also check for speaker_map.json as diarization indicator
            if not is_diarized and (entry / "speaker_map.json").exists():
                is_diarized = True

        sessions.append(SessionInfo(
            path=entry,
            start_datetime=dt,
            display_label=label,
            has_mic_wav=has_mic,
            loopback_files=loopback_files,
            transcript_versions=versions,
            duration_sec=duration_sec,
            is_diarized=is_diarized,
        ))

    sessions.sort(key=lambda s: s.start_datetime, reverse=True)
    return sessions


# ---------------------------------------------------------------------------
# TranscriptReviewer
# ---------------------------------------------------------------------------

class TranscriptReviewer:
    """Transcript review and re-transcription window.

    Parameters
    ----------
    root:
        The main tkinter ``Tk`` instance (from the caption overlay).
        The reviewer is created as a ``Toplevel`` child of this root.
    output_dir:
        Path to the output directory containing session folders.
    on_retranscribe:
        Callback invoked with the session directory ``Path`` when the
        user clicks "Re-transcribe".
    on_cancel_retranscribe:
        Callback to cancel a running re-transcription.
    """

    def __init__(
        self,
        root: tk.Tk,
        output_dir: Path,
        on_retranscribe: Callable[[Path], None],
        on_cancel_retranscribe: Callable[[], None],
        on_diarize: Optional[Callable[[Path, str, str, str, Optional[int]], None]] = None,
        on_cancel_diarize: Optional[Callable[[], None]] = None,
        on_save_speaker_names: Optional[Callable[[Path, dict[str, str]], None]] = None,
        on_hf_token_changed: Optional[Callable[[str], None]] = None,
        on_save_geometry: Optional[Callable[[str], None]] = None,
        tutorial_show_on_open: bool = True,
        on_tutorial_preference_changed: Optional[Callable[[bool], None]] = None,
    ) -> None:
        self._root = root
        self._output_dir = output_dir
        self._on_retranscribe = on_retranscribe
        self._on_cancel_retranscribe = on_cancel_retranscribe
        self._on_diarize = on_diarize
        self._on_cancel_diarize = on_cancel_diarize
        self._on_save_speaker_names = on_save_speaker_names
        self._on_hf_token_changed = on_hf_token_changed
        self._on_save_geometry = on_save_geometry
        self._on_tutorial_preference_changed = on_tutorial_preference_changed

        self._sessions: list[SessionInfo] = []
        self._filtered_sessions: list[SessionInfo] = []
        self._selected_session: Optional[SessionInfo] = None
        self._session_rows: dict[str, list[tk.Widget]] = {}  # path_str -> [row, line1_frame, ...]
        self._batch_running: bool = False
        self._diarize_running: bool = False
        self._recording_active: bool = False
        self._installing_package: bool = False

        # Edit mode state
        self._edit_mode: bool = False
        self._displayed_segments: list[TranscriptSegment] = []
        self._displayed_version: Optional[str] = None

        # Diarization backend state
        self._initial_backend: str = "speechbrain"
        self._hf_token: str = ""

        # Speaker panel state
        self._current_speaker_infos: Optional[list[SpeakerInfo]] = None
        self._speaker_name_entries: dict[str, tk.Entry] = {}

        # Window state
        self._window: Optional[tk.Toplevel] = None
        self._visible: bool = False
        self._initial_geometry: Optional[str] = None
        self._tutorial_show_on_open: bool = tutorial_show_on_open
        self._tutorial_backdrop: Optional[tk.Toplevel] = None
        self._tutorial_window: Optional[tk.Toplevel] = None
        self._tutorial_step_idx: int = 0
        self._tutorial_show_var = tk.BooleanVar(value=tutorial_show_on_open)

    # ------------------------------------------------------------------
    # Show / Hide
    # ------------------------------------------------------------------

    def show(self) -> None:
        """Show the reviewer window, creating it if needed."""
        was_hidden = not self._visible
        if self._window is None or not self._window.winfo_exists():
            self._create_window()
            self._refresh_sessions()
        self._window.deiconify()
        self._window.lift()
        self._visible = True
        if was_hidden and self._tutorial_show_on_open:
            self._window.after(150, lambda: self._open_tutorial(force=True))

    def hide(self) -> None:
        """Hide the reviewer window (does not destroy it)."""
        self._close_tutorial()
        if self._window is not None and self._window.winfo_exists():
            self._save_window_geometry()
            self._window.withdraw()
        self._visible = False

    @property
    def visible(self) -> bool:
        return self._visible

    def set_initial_geometry(self, geometry: Optional[str]) -> None:
        """Set the initial window geometry from saved config."""
        self._initial_geometry = geometry

    # ------------------------------------------------------------------
    # State updates from main.py
    # ------------------------------------------------------------------

    def set_recording_active(self, active: bool) -> None:
        """Update recording state — disables Re-transcribe during recording."""
        self._recording_active = active
        self._update_button_state()

    def set_batch_running(self, running: bool) -> None:
        """Update batch state — changes button text and progress bar."""
        self._batch_running = running
        self._update_button_state()
        if not running:
            self._progress_bar["value"] = 0
            self._status_label.configure(text="Ready")

    def update_progress(self, progress: BatchProgress) -> None:
        """Update the progress bar and status label (call on main thread)."""
        self._progress_bar["value"] = progress.percent
        detail = f" - {progress.detail}" if progress.detail else ""
        self._status_label.configure(text=f"{progress.phase}{detail}")

    def set_diarize_running(self, running: bool) -> None:
        """Update diarize state -- changes button text and progress bar."""
        self._diarize_running = running
        self._update_button_state()
        if not running:
            # Stop indeterminate animation and reset to determinate
            self._progress_bar.stop()
            self._progress_bar.configure(mode="determinate")
            self._progress_bar["value"] = 0
            self._status_label.configure(text="Ready")

    def update_diarize_progress(self, progress: DiarizeProgress) -> None:
        """Update the progress bar and status label for diarization."""
        # Switch from indeterminate (spinner) to determinate (percentage)
        # on the first real progress callback
        if str(self._progress_bar.cget("mode")) == "indeterminate":
            self._progress_bar.stop()
            self._progress_bar.configure(mode="determinate")
        self._progress_bar["value"] = progress.percent
        detail = f" - {progress.detail}" if progress.detail else ""
        self._status_label.configure(text=f"{progress.phase}{detail}")

    def on_retranscribe_complete(self) -> None:
        """Called when batch transcription finishes successfully."""
        self._batch_running = False
        self._update_button_state()
        self._progress_bar["value"] = 100
        self._status_label.configure(text="Complete")

        # Refresh the version dropdown for the current session
        if self._selected_session is not None:
            self._selected_session.transcript_versions = discover_versions(
                self._selected_session.path
            )
            self._populate_version_dropdown()
            # Select the newest version
            versions = self._selected_session.transcript_versions
            if versions:
                self._version_var.set(
                    self._version_display_name_with_context(versions[-1])
                )
                self._on_version_changed()

    def on_diarize_complete(
        self, speaker_infos: list[SpeakerInfo],
    ) -> None:
        """Called when diarization finishes -- shows the inline speaker panel."""
        self._diarize_running = False
        self._update_button_state()
        self._progress_bar.stop()
        self._progress_bar.configure(mode="determinate")
        self._progress_bar["value"] = 100
        self._status_label.configure(text="Diarization complete")

        # Refresh versions and select newest
        if self._selected_session is not None:
            self._selected_session.transcript_versions = discover_versions(
                self._selected_session.path
            )
            self._populate_version_dropdown()
            versions = self._selected_session.transcript_versions
            if versions:
                self._version_var.set(
                    self._version_display_name_with_context(versions[-1])
                )
                self._on_version_changed()

        # Show inline speaker naming panel
        if speaker_infos and self._window is not None:
            self._show_speaker_panel(speaker_infos)

    def refresh(self) -> None:
        """Refresh the session list (e.g. after a new recording is saved)."""
        if self._window is not None and self._window.winfo_exists():
            self._refresh_sessions()

    def refresh_current_version(self) -> None:
        """Refresh the version dropdown and re-display the current version.

        Public method for main.py to call after speaker names are saved,
        avoiding direct access to private members.
        """
        if self._selected_session is None:
            return

        self._selected_session.transcript_versions = discover_versions(
            self._selected_session.path
        )
        self._populate_version_dropdown()
        versions = self._selected_session.transcript_versions
        if versions:
            self._version_var.set(
                self._version_display_name_with_context(versions[-1])
            )
            self._on_version_changed()

    # ------------------------------------------------------------------
    # Window construction
    # ------------------------------------------------------------------

    def _create_window(self) -> None:
        """Build the reviewer Toplevel window."""
        self._window = tk.Toplevel(self._root)
        self._window.title("Tachyon Transcripts \u2014 Review")
        self._window.configure(bg=Color.bg_primary)
        self._window.minsize(Dim.reviewer_min_width, Dim.reviewer_min_height)
        self._window.protocol("WM_DELETE_WINDOW", self._on_window_close)

        # Set window icon from Pillow-generated app icon
        try:
            from PIL import ImageTk
            from tachyon.ui.tray import create_app_icon
            icon_img = create_app_icon(recording=False)
            self._icon_photo = ImageTk.PhotoImage(icon_img)
            self._window.iconphoto(False, self._icon_photo)
        except Exception:
            pass  # Non-critical — fall back to default tkinter icon

        # Restore saved geometry or use default
        if self._initial_geometry and self._is_geometry_on_screen(self._initial_geometry):
            self._window.geometry(self._initial_geometry)
        else:
            self._window.geometry(
                f"{Dim.reviewer_default_width}x{Dim.reviewer_default_height}"
            )

        # -- Top toolbar: buttons + progress ---------------------------------
        self._build_toolbar()

        # Bottom status bar
        status_bar = tk.Frame(self._window, bg=Color.bg_elevated, height=28)
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)
        status_bar.pack_propagate(False)
        tk.Frame(self._window, bg=Color.divider, height=1).pack(fill=tk.X, side=tk.BOTTOM)

        self._statusbar_left = tk.Label(
            status_bar, text="",
            font=(Font.family, Font.size_caption),
            fg=Color.fg_muted, bg=Color.bg_elevated,
            anchor=tk.W,
        )
        self._statusbar_left.pack(side=tk.LEFT, padx=12)

        tk.Label(
            status_bar, text="Ctrl+E Edit  |  Ctrl+R Transcribe",
            font=(Font.family, Font.size_caption),
            fg=Color.fg_dim, bg=Color.bg_elevated,
            anchor=tk.E,
        ).pack(side=tk.RIGHT, padx=12)

        # Main horizontal paned layout
        main_pane = tk.PanedWindow(
            self._window, orient=tk.HORIZONTAL,
            bg=Color.border, sashwidth=1, sashrelief=tk.FLAT,
        )
        main_pane.pack(fill=tk.BOTH, expand=True)

        # -- Left panel: session list ----------------------------------------
        left_frame = tk.Frame(main_pane, bg=Color.bg_secondary)

        # Header with accent underline
        header_container = tk.Frame(left_frame, bg=Color.bg_elevated)
        header_container.pack(fill=tk.X)
        left_header = tk.Label(
            header_container, text="Sessions",
            font=(Font.family, Font.size_header, "bold"),
            fg=Color.fg_primary, bg=Color.bg_elevated,
            anchor=tk.W, padx=14, pady=10,
        )
        left_header.pack(fill=tk.X)
        tk.Frame(header_container, bg=Color.glow_primary, height=2).pack(fill=tk.X)

        # Search / filter entry with focus glow
        search_frame = tk.Frame(
            left_frame, bg=Color.border_subtle,
            highlightthickness=1,
            highlightbackground=Color.border_subtle,
            highlightcolor=Color.accent,
        )
        search_frame.pack(fill=tk.X, padx=10, pady=(10, 6))

        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._filter_sessions())
        self._search_entry = tk.Entry(
            search_frame,
            textvariable=self._search_var,
            font=(Font.family, Font.size_small),
            fg=Color.fg_primary, bg=Color.bg_input,
            insertbackground=Color.fg_primary,
            relief=tk.FLAT,
            highlightthickness=0,
        )
        self._search_entry.pack(fill=tk.X, ipady=5, padx=1, pady=1)
        self._search_entry.insert(0, "")
        # Placeholder text
        self._search_entry.bind("<FocusIn>", self._on_search_focus_in)
        self._search_entry.bind("<FocusOut>", self._on_search_focus_out)
        # Focus glow on the parent frame
        self._search_entry.bind("<FocusIn>", lambda e: search_frame.configure(highlightbackground=Color.accent), add="+")
        self._search_entry.bind("<FocusOut>", lambda e: search_frame.configure(highlightbackground=Color.border_subtle), add="+")
        self._show_search_placeholder()

        # Session list — custom scrollable frame for rich entries
        list_container = tk.Frame(left_frame, bg=Color.bg_secondary)
        list_container.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        self._session_canvas = tk.Canvas(
            list_container, bg=Color.bg_secondary,
            highlightthickness=0, borderwidth=0,
        )
        self._session_scrollbar = tk.Scrollbar(
            list_container, command=self._session_canvas.yview,
            bg=Color.bg_secondary, troughcolor=Color.bg_primary,
        )
        self._session_list_frame = tk.Frame(
            self._session_canvas, bg=Color.bg_secondary,
        )

        self._session_list_frame.bind(
            "<Configure>",
            lambda e: self._session_canvas.configure(
                scrollregion=self._session_canvas.bbox("all")
            ),
        )
        self._session_canvas_window = self._session_canvas.create_window(
            (0, 0), window=self._session_list_frame, anchor=tk.NW,
        )
        self._session_canvas.configure(yscrollcommand=self._session_scrollbar.set)

        self._session_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._session_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Make canvas window expand to fill width
        self._session_canvas.bind("<Configure>", self._on_canvas_configure)

        # Mousewheel scrolling for session list
        self._session_canvas.bind("<Enter>", self._bind_mousewheel)
        self._session_canvas.bind("<Leave>", self._unbind_mousewheel)

        main_pane.add(left_frame, minsize=Dim.sidebar_min_width, width=Dim.sidebar_default_width)

        # -- Right panel: transcript viewer ----------------------------------
        right_frame = tk.Frame(main_pane, bg=Color.bg_primary)

        # Header area
        header_frame = tk.Frame(right_frame, bg=Color.bg_elevated)
        header_frame.pack(fill=tk.X)

        # Two-line title area
        title_area = tk.Frame(header_frame, bg=Color.bg_elevated)
        title_area.pack(side=tk.LEFT, padx=16, pady=8)

        self._session_title = tk.Label(
            title_area, text="Select a session",
            font=(Font.family, 16, "bold"),
            fg=Color.fg_bright, bg=Color.bg_elevated,
            anchor=tk.W,
        )
        self._session_title.pack(fill=tk.X)

        self._session_subtitle = tk.Label(
            title_area, text="",
            font=(Font.family, Font.size_small),
            fg=Color.fg_secondary, bg=Color.bg_elevated,
            anchor=tk.W,
        )
        self._session_subtitle.pack(fill=tk.X)

        # Version selector area (right side of header)
        version_frame = tk.Frame(header_frame, bg=Color.bg_elevated)
        version_frame.pack(side=tk.RIGHT, padx=16, pady=8)

        # "Edit Speakers" button (hidden by default, shown for diarized versions)
        self._edit_speakers_btn = HoverButton(
            version_frame, text="Edit Speakers",
            fg=Color.fg_bright, bg=Color.purple,
            hover_bg=Color.purple_hover,
            font=(Font.family, Font.size_small),
            padx=10, pady=3,
            command=self._on_edit_speakers_click,
        )
        # Not packed yet — shown dynamically in _on_version_changed

        tk.Label(
            version_frame, text="Version:",
            font=(Font.family, Font.size_body),
            fg=Color.fg_secondary, bg=Color.bg_elevated,
        ).pack(side=tk.LEFT, padx=(0, 6))

        self._version_var = tk.StringVar(value="")
        self._version_dropdown = ttk.Combobox(
            version_frame,
            textvariable=self._version_var,
            state="readonly",
            width=25,
            font=(Font.family, Font.size_small),
        )
        self._version_dropdown.pack(side=tk.LEFT)
        self._version_dropdown.bind("<<ComboboxSelected>>", lambda e: self._on_version_changed())

        # Accent divider between header and content
        tk.Frame(right_frame, bg=Color.divider, height=1).pack(fill=tk.X)

        # -- Speaker panel (inline, between header and transcript) -----------
        self._speaker_panel_frame = tk.Frame(right_frame, bg=Color.speaker_panel_bg)
        # Not packed — starts hidden. _show_speaker_panel() will pack it.

        self._speaker_panel_header = tk.Label(
            self._speaker_panel_frame,
            text="",
            font=(Font.family, Font.size_body, "bold"),
            fg=Color.fg_primary, bg=Color.speaker_panel_bg,
            anchor=tk.W, padx=10, pady=6,
        )
        self._speaker_panel_header.pack(fill=tk.X)

        # Scrollable area for speaker entries
        self._speaker_entries_frame = tk.Frame(
            self._speaker_panel_frame, bg=Color.speaker_panel_bg,
        )
        self._speaker_entries_frame.pack(fill=tk.X, padx=6, pady=(0, 4))

        # Button row for speaker panel
        speaker_btn_frame = tk.Frame(self._speaker_panel_frame, bg=Color.speaker_panel_bg)
        speaker_btn_frame.pack(fill=tk.X, padx=10, pady=(0, 8))

        self._save_names_btn = HoverButton(
            speaker_btn_frame, text="Save Names",
            fg=Color.fg_bright, bg=Color.purple,
            hover_bg=Color.purple_hover,
            padx=14, pady=5,
            command=self._on_save_names_click,
        )
        self._save_names_btn.pack(side=tk.LEFT, padx=(0, 8))

        self._close_panel_btn = HoverButton(
            speaker_btn_frame, text="Close",
            fg=Color.fg_bright, bg=Color.btn_neutral,
            hover_bg=Color.btn_neutral_hover,
            padx=14, pady=5,
            command=self._hide_speaker_panel,
        )
        self._close_panel_btn.pack(side=tk.LEFT)

        # Transcript text widget
        text_frame = tk.Frame(right_frame, bg=Color.bg_surface)
        text_frame.pack(fill=tk.BOTH, expand=True)

        self._text_widget = tk.Text(
            text_frame,
            font=(Font.family, Font.size_body),
            fg=Color.fg_primary,
            bg=Color.bg_surface,
            wrap=tk.WORD,
            state=tk.DISABLED,
            borderwidth=0,
            highlightthickness=0,
            padx=24,
            pady=16,
            spacing3=10,
            cursor="arrow",
            selectbackground=Color.selection,
            selectforeground=Color.fg_primary,
        )

        scrollbar = tk.Scrollbar(text_frame, command=self._text_widget.yview)
        self._text_widget.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._text_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Configure text tags
        self._text_widget.tag_configure(
            "speaker_you",
            foreground=Color.speaker_you,
            font=(Font.family, Font.size_body, "bold"),
        )
        self._text_widget.tag_configure(
            "speaker_them",
            foreground=Color.speaker_them,
            font=(Font.family, Font.size_body, "bold"),
        )
        self._text_widget.tag_configure(
            "timestamp",
            foreground=Color.timestamp,
            font=(Font.family_mono, Font.size_small),
        )
        self._text_widget.tag_configure("text_normal", foreground=Color.fg_primary)
        self._text_widget.tag_configure(
            "segment_border",
            lmargin1=4, lmargin2=4,
        )

        main_pane.add(right_frame)

        # -- Keyboard bindings -----------------------------------------------
        self._window.bind("<Control-e>", lambda e: self._toggle_edit_mode())
        self._window.bind("<Control-r>", lambda e: self._on_retranscribe_click())
        self._window.bind("<Escape>", self._on_escape)

        self._update_button_state()

    def _build_toolbar(self) -> None:
        """Build the top toolbar with grouped controls."""
        toolbar_canvas = GradientBar(
            self._window,
            color_start=Color.bg_elevated,
            color_end="#0d1322",
            height=Dim.toolbar_height,
        )
        toolbar_canvas.pack(fill=tk.X, side=tk.TOP)
        toolbar = toolbar_canvas.interior
        toolbar.pack_propagate(False)
        toolbar.configure(height=Dim.toolbar_height)

        # == Left group: Action buttons ==
        action_frame = tk.Frame(toolbar, bg=Color.bg_elevated)
        action_frame.pack(side=tk.LEFT, padx=(12, 0))

        self._retranscribe_btn = tk.Button(
            action_frame,
            text="\u21bb  Re-transcribe",
            font=(Font.family, Font.size_btn),
            fg=Color.fg_bright,
            bg=Color.accent,
            activebackground=Color.accent_hover,
            activeforeground=Color.fg_bright,
            relief=tk.FLAT,
            padx=12,
            cursor="hand2",
            command=self._on_retranscribe_click,
        )
        self._retranscribe_btn.pack(side=tk.LEFT, padx=(0, 8), pady=9)

        self._diarize_btn = tk.Button(
            action_frame,
            text="\U0001f5e3  Identify Speakers",
            font=(Font.family, Font.size_btn),
            fg=Color.fg_bright,
            bg=Color.purple,
            activebackground=Color.purple_hover,
            activeforeground=Color.fg_bright,
            relief=tk.FLAT,
            padx=12,
            cursor="hand2",
            command=self._on_diarize_click,
        )
        self._diarize_btn.pack(side=tk.LEFT, padx=(0, 8), pady=9)

        self._edit_btn = tk.Button(
            action_frame,
            text="\u270e  Edit",
            font=(Font.family, Font.size_btn),
            fg=Color.fg_bright,
            bg=Color.success,
            activebackground=Color.success_hover,
            activeforeground=Color.fg_bright,
            relief=tk.FLAT,
            padx=12,
            cursor="hand2",
            command=self._toggle_edit_mode,
        )
        self._edit_btn.pack(side=tk.LEFT, padx=(0, 4), pady=9)

        # Separator
        tk.Frame(toolbar, bg=Color.border, width=1).pack(
            side=tk.LEFT, fill=tk.Y, padx=10, pady=8,
        )

        # == Center: Progress + Status ==
        center_frame = tk.Frame(toolbar, bg=Color.bg_elevated)
        center_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

        style = ttk.Style()
        style.theme_use("default")
        style.configure(
            "Batch.Horizontal.TProgressbar",
            background=Color.accent,
            troughcolor=Color.progress_trough,
            borderwidth=0,
        )
        # Global dark theme for comboboxes
        style.configure(
            "TCombobox",
            fieldbackground=Color.bg_input,
            background=Color.bg_elevated,
            foreground=Color.fg_primary,
            selectbackground=Color.selection,
            selectforeground=Color.fg_primary,
            borderwidth=0,
        )
        style.map("TCombobox",
            fieldbackground=[("readonly", Color.bg_input)],
            selectbackground=[("readonly", Color.selection)],
        )
        # Dark scrollbar
        style.configure(
            "Vertical.TScrollbar",
            background=Color.bg_elevated,
            troughcolor=Color.scrollbar_trough,
            arrowcolor=Color.fg_dim,
            borderwidth=0,
        )
        style.map("Vertical.TScrollbar",
            background=[("active", Color.btn_neutral_hover)],
        )

        self._progress_bar = ttk.Progressbar(
            center_frame,
            style="Batch.Horizontal.TProgressbar",
            orient=tk.HORIZONTAL,
            mode="determinate",
            maximum=100,
        )
        self._progress_bar.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=12)

        self._status_label = tk.Label(
            center_frame, text="Ready",
            font=(Font.family, Font.size_small),
            fg=Color.fg_secondary, bg=Color.bg_elevated,
        )
        self._status_label.pack(side=tk.LEFT, padx=(10, 0))

        # Separator
        tk.Frame(toolbar, bg=Color.border, width=1).pack(
            side=tk.LEFT, fill=tk.Y, padx=10, pady=8,
        )

        # == Right group: Config controls ==
        config_frame = tk.Frame(toolbar, bg=Color.bg_elevated)
        config_frame.pack(side=tk.RIGHT, padx=(0, 12))

        self._backend_var = tk.StringVar(
            value=self._initial_backend if self._initial_backend in _BACKEND_OPTIONS else "speechbrain",
        )
        self._backend_dropdown = ttk.Combobox(
            config_frame,
            textvariable=self._backend_var,
            state="readonly",
            values=list(_BACKEND_OPTIONS),
            width=12,
            font=(Font.family, Font.size_tiny),
        )
        self._backend_dropdown.pack(side=tk.RIGHT, padx=(0, 0), pady=7)
        self._backend_dropdown.bind("<<ComboboxSelected>>", lambda e: self._on_backend_changed())

        tk.Label(
            config_frame, text="Backend:",
            font=(Font.family, Font.size_tiny),
            fg=Color.fg_secondary, bg=Color.bg_elevated,
        ).pack(side=tk.RIGHT, padx=(6, 0), pady=7)

        self._speaker_count_var = tk.StringVar(value="Auto")
        self._speaker_count_dropdown = ttk.Combobox(
            config_frame,
            textvariable=self._speaker_count_var,
            state="readonly",
            values=["Auto", "2", "3", "4", "5", "6", "7", "8"],
            width=5,
            font=(Font.family, Font.size_tiny),
        )
        self._speaker_count_dropdown.pack(side=tk.RIGHT, padx=(6, 0), pady=7)

        tk.Label(
            config_frame, text="Speakers:",
            font=(Font.family, Font.size_tiny),
            fg=Color.fg_secondary, bg=Color.bg_elevated,
        ).pack(side=tk.RIGHT, padx=(6, 0), pady=7)

        # HF Token management button (only visible when pyannote is selected)
        self._token_btn = tk.Button(
            config_frame,
            text=self._token_button_text(),
            font=(Font.family, Font.size_tiny),
            fg=Color.fg_primary,
            bg=Color.btn_neutral,
            activebackground=Color.btn_neutral_hover,
            activeforeground=Color.fg_bright,
            relief=tk.FLAT,
            padx=6,
            cursor="hand2",
            command=self._on_token_btn_click,
        )
        # Only show if pyannote is the initial backend
        if self._initial_backend == "pyannote":
            self._token_btn.pack(side=tk.RIGHT, padx=(6, 0), pady=7)

        open_folder_btn = HoverButton(
            config_frame,
            text="Open Folder",
            icon="\U0001f4c2",
            fg=Color.fg_primary,
            bg=Color.btn_neutral,
            hover_bg=Color.btn_neutral_hover,
            font=(Font.family, Font.size_tiny),
            padx=10,
            pady=4,
            command=self._on_open_folder,
        )
        open_folder_btn.pack(side=tk.RIGHT, padx=(8, 0), pady=9)

        help_btn = HoverButton(
            config_frame,
            text="Help",
            icon="?",
            fg=Color.fg_primary,
            bg=Color.btn_neutral,
            hover_bg=Color.btn_neutral_hover,
            font=(Font.family, Font.size_tiny),
            padx=10,
            pady=4,
            command=self._on_help_click,
        )
        help_btn.pack(side=tk.RIGHT, padx=(8, 0), pady=9)

        # -- Tooltips --
        ToolTip(self._retranscribe_btn, "Re-transcribe with higher quality settings (Ctrl+R)")
        ToolTip(self._diarize_btn, "Run speaker diarization to identify who said what")
        ToolTip(self._edit_btn, "Toggle inline editing of the transcript (Ctrl+E)")
        ToolTip(self._backend_dropdown, "Select diarization engine")
        ToolTip(self._speaker_count_dropdown, "Expected number of speakers (Auto = let the engine decide)")
        ToolTip(open_folder_btn, "Open the session folder in Explorer")
        ToolTip(help_btn, "Explain this screen")

        # 1px divider below toolbar
        tk.Frame(self._window, bg=Color.border, height=1).pack(fill=tk.X)

    # ------------------------------------------------------------------
    # Search / filter
    # ------------------------------------------------------------------

    def _show_search_placeholder(self) -> None:
        """Show placeholder text in search entry."""
        if not self._search_var.get():
            self._search_entry.configure(fg=Color.fg_muted)
            self._search_entry.delete(0, tk.END)
            self._search_entry.insert(0, "Search sessions...")

    def _on_search_focus_in(self, event: tk.Event) -> None:
        if self._search_entry.get() == "Search sessions...":
            self._search_entry.delete(0, tk.END)
            self._search_entry.configure(fg=Color.fg_primary)

    def _on_search_focus_out(self, event: tk.Event) -> None:
        if not self._search_entry.get():
            self._show_search_placeholder()

    def _filter_sessions(self) -> None:
        """Filter sessions based on search text."""
        query = self._search_var.get().strip().lower()
        if not query or query == "search sessions...":
            self._filtered_sessions = list(self._sessions)
        else:
            self._filtered_sessions = [
                s for s in self._sessions
                if query in s.start_datetime.strftime("%b %d %Y %I:%M %p").lower()
                or query in s.display_label.lower()
                or query in s.start_datetime.strftime("%Y-%m-%d").lower()
            ]
        self._rebuild_session_list()

    # ------------------------------------------------------------------
    # Session list (rich entries)
    # ------------------------------------------------------------------

    def _on_canvas_configure(self, event: tk.Event) -> None:
        """Resize session list frame to fill canvas width."""
        self._session_canvas.itemconfig(
            self._session_canvas_window, width=event.width,
        )

    def _bind_mousewheel(self, event: tk.Event) -> None:
        self._session_canvas.bind_all("<MouseWheel>", self._on_session_mousewheel)

    def _unbind_mousewheel(self, event: tk.Event) -> None:
        self._session_canvas.unbind_all("<MouseWheel>")

    def _on_session_mousewheel(self, event: tk.Event) -> None:
        self._session_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _rebuild_session_list(self) -> None:
        """Rebuild the session list UI from filtered sessions."""
        # Destroy old widgets
        for widget in self._session_list_frame.winfo_children():
            widget.destroy()
        self._session_rows.clear()

        for i, session in enumerate(self._filtered_sessions):
            self._create_session_row(session, i)

        # Update status bar count
        if hasattr(self, "_statusbar_left"):
            n = len(self._filtered_sessions)
            self._statusbar_left.configure(
                text=f"{n} session{'s' if n != 1 else ''}"
            )

    def _create_session_row(self, session: SessionInfo, index: int) -> None:
        """Create a rich session list entry using SessionCard."""
        is_selected = (
            self._selected_session is not None
            and session.path == self._selected_session.path
        )

        # Format date nicely
        date_text = session.start_datetime.strftime("%b %d \u2014 %I:%M %p")

        # Build meta text
        meta_parts: list[str] = []
        if session.duration_sec is not None:
            mins = int(session.duration_sec) // 60
            if mins > 0:
                meta_parts.append(f"{mins} min")
            else:
                secs = int(session.duration_sec)
                meta_parts.append(f"{secs} sec")

        num_versions = len(session.transcript_versions)
        if num_versions > 1:
            meta_parts.append(f"{num_versions} versions")
        elif num_versions == 1:
            meta_parts.append("1 version")

        meta_text = "  \u00b7  ".join(meta_parts) if meta_parts else ""

        card = SessionCard(
            self._session_list_frame,
            date_text=date_text,
            meta_text=meta_text,
            is_diarized=session.is_diarized,
            on_click=lambda s=session: self._on_session_row_click(s),
        )
        card.pack(fill=tk.X, pady=(0, Dim.card_gap))

        if is_selected:
            card.set_selected(True)

        # Track the card widget for selection updates
        self._session_rows[str(session.path)] = [card]

    def _update_session_selection(self) -> None:
        """Update selection highlight on existing session rows without rebuilding."""
        for path_str, widgets in self._session_rows.items():
            is_selected = (
                self._selected_session is not None
                and str(self._selected_session.path) == path_str
            )
            for widget in widgets:
                if isinstance(widget, SessionCard):
                    widget.set_selected(is_selected)
                else:
                    bg = Color.bg_card_selected if is_selected else Color.bg_card
                    try:
                        widget.configure(bg=bg)
                    except tk.TclError:
                        pass

    def _on_session_row_click(self, session: SessionInfo) -> None:
        """Handle click on a session row."""
        # Guard: confirm discard if editing
        if self._edit_mode:
            if not messagebox.askyesno(
                "Unsaved Edits",
                "Discard unsaved edits?",
                parent=self._window,
            ):
                return
            self._exit_edit_mode(save=False)

        self._selected_session = session
        self._update_session_selection()  # Update highlight without rebuilding

        # Two-line title: date on top, time + metadata below
        self._session_title.configure(
            text=session.start_datetime.strftime("%B %d, %Y")
        )
        time_str = session.start_datetime.strftime("%I:%M %p")
        meta_parts = [time_str]
        if session.duration_sec is not None:
            mins = int(session.duration_sec) // 60
            meta_parts.append(f"{mins} min" if mins > 0 else f"{int(session.duration_sec)} sec")
        num_v = len(session.transcript_versions)
        if num_v > 0:
            meta_parts.append(f"{num_v} version{'s' if num_v > 1 else ''}")
        self._session_subtitle.configure(
            text="  \u00b7  ".join(meta_parts)
        )

        # Hide speaker panel when switching sessions
        self._hide_speaker_panel()

        self._populate_version_dropdown()
        self._update_button_state()

        # Load the first available transcript
        if session.transcript_versions:
            self._version_var.set(
                self._version_display_name(session.transcript_versions[0])
            )
            self._on_version_changed()
        else:
            self._clear_transcript()
            self._show_message("No transcript found for this session.")

    # ------------------------------------------------------------------
    # Session list management
    # ------------------------------------------------------------------

    def _refresh_sessions(self) -> None:
        """Rescan the output directory and populate the session list."""
        self._sessions = discover_sessions(self._output_dir)
        self._filtered_sessions = list(self._sessions)
        self._rebuild_session_list()

        # Auto-select the first (newest) session if available
        if self._sessions:
            self._on_session_row_click(self._sessions[0])

    def _populate_version_dropdown(self) -> None:
        """Fill the version dropdown with available transcript files."""
        if self._selected_session is None:
            self._version_dropdown["values"] = []
            return

        display_names = [
            self._version_display_name_with_context(v)
            for v in self._selected_session.transcript_versions
        ]
        self._version_dropdown["values"] = display_names

    def _on_version_changed(self) -> None:
        """Load the selected transcript version."""
        if self._selected_session is None:
            return

        # Guard: confirm discard if editing
        if self._edit_mode:
            if not messagebox.askyesno(
                "Unsaved Edits",
                "Discard unsaved edits?",
                parent=self._window,
            ):
                return
            self._exit_edit_mode(save=False)

        display = self._version_var.get()
        filename = self._display_name_to_filename(display)
        if not filename:
            return

        self._displayed_version = filename
        transcript_path = self._selected_session.path / filename
        if not transcript_path.exists():
            self._clear_transcript()
            self._show_message(f"File not found: {filename}")
            return

        # Check if this is a diarized version with a speaker_map
        is_diarized = False
        try:
            head = transcript_path.read_text(encoding="utf-8")[:500]
            is_diarized = "Diarized" in head
        except Exception:
            pass

        # Show/hide Edit Speakers button
        if is_diarized and load_speaker_map(self._selected_session.path) is not None:
            self._edit_speakers_btn.pack(side=tk.RIGHT, padx=(0, 12))
        else:
            self._edit_speakers_btn.pack_forget()

        try:
            metadata, segments = load_transcript_from_markdown(transcript_path)
            self._display_segments(segments)
        except Exception as e:
            logger.exception("Failed to load transcript %s", transcript_path)
            self._clear_transcript()
            self._show_message(f"Error loading transcript: {e}")

    # ------------------------------------------------------------------
    # Inline speaker panel
    # ------------------------------------------------------------------

    def _show_speaker_panel(self, speaker_infos: list[SpeakerInfo]) -> None:
        """Show the inline speaker naming panel with detected speakers."""
        self._current_speaker_infos = speaker_infos
        self._speaker_name_entries.clear()

        # Clear previous speaker entries
        for widget in self._speaker_entries_frame.winfo_children():
            widget.destroy()

        # Header
        self._speaker_panel_header.configure(
            text=f"{len(speaker_infos)} speaker(s) detected"
        )

        # Create a row per speaker
        for info in speaker_infos:
            row = tk.Frame(self._speaker_entries_frame, bg=Color.bg_secondary, padx=8, pady=5)
            row.pack(fill=tk.X, pady=2)

            # Color dot
            color = SPEAKER_COLORS[info.color_index % len(SPEAKER_COLORS)]
            dot = tk.Canvas(row, width=12, height=12, bg=Color.bg_secondary, highlightthickness=0)
            dot.create_oval(1, 1, 11, 11, fill=color, outline=color)
            dot.pack(side=tk.LEFT, padx=(0, 6))

            # Speaker label
            tk.Label(
                row, text=info.display_name,
                font=(Font.family, Font.size_body, "bold"),
                fg=color, bg=Color.bg_secondary,
            ).pack(side=tk.LEFT)

            # Duration + segment count
            mins = int(info.total_duration) // 60
            secs = int(info.total_duration) % 60
            dur_text = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
            tk.Label(
                row, text=f"({info.segment_count} seg, {dur_text})",
                font=(Font.family, Font.size_tiny), fg=Color.fg_muted, bg=Color.bg_secondary,
            ).pack(side=tk.LEFT, padx=(6, 0))

            # Sample text (truncated)
            if info.sample_texts:
                tk.Label(
                    row, text=f'  "{info.sample_texts[0]}"',
                    font=(Font.family, Font.size_tiny, "italic"),
                    fg=Color.fg_dim, bg=Color.bg_secondary,
                    anchor=tk.W,
                ).pack(side=tk.LEFT, padx=(6, 0), fill=tk.X, expand=True)

            # Name entry
            tk.Label(
                row, text="Name:",
                font=(Font.family, Font.size_tiny), fg=Color.fg_primary, bg=Color.bg_secondary,
            ).pack(side=tk.LEFT, padx=(8, 4))

            entry = tk.Entry(
                row, font=(Font.family, Font.size_body),
                fg=Color.fg_primary, bg=Color.bg_input,
                insertbackground=Color.fg_primary,
                relief=tk.FLAT, width=15,
            )
            entry.insert(0, info.display_name)
            entry.pack(side=tk.LEFT)
            self._speaker_name_entries[info.id] = entry

        # Show the panel (pack before the text widget)
        self._speaker_panel_frame.pack(fill=tk.X, before=self._text_widget.master)

    def _hide_speaker_panel(self) -> None:
        """Hide the inline speaker naming panel."""
        self._speaker_panel_frame.pack_forget()
        self._current_speaker_infos = None
        self._speaker_name_entries.clear()

    def _on_save_names_click(self) -> None:
        """Handle Save Names button — collect entries and fire callback."""
        if not self._current_speaker_infos or not self._on_save_speaker_names:
            return
        if self._selected_session is None:
            return

        names: dict[str, str] = {}
        for info in self._current_speaker_infos:
            entry = self._speaker_name_entries.get(info.id)
            if entry:
                val = entry.get().strip()
                names[info.id] = val if val else info.display_name
            else:
                names[info.id] = info.display_name

        self._hide_speaker_panel()
        self._on_save_speaker_names(self._selected_session.path, names)

    def _on_edit_speakers_click(self) -> None:
        """Handle the 'Edit Speakers' button — rebuild speaker panel from map."""
        if self._selected_session is None:
            return

        map_data = load_speaker_map(self._selected_session.path)
        if map_data is None:
            return

        speaker_map = map_data.get("speaker_map", {})
        num_detected = map_data.get("num_speakers_detected", 0)

        # Build minimal SpeakerInfo list from the map
        infos: list[SpeakerInfo] = []
        for speaker_id in sorted(speaker_map.keys()):
            try:
                num = int(speaker_id.split("_")[-1])
            except (ValueError, IndexError):
                num = 0

            color_idx = min(num, len(SPEAKER_COLORS) - 1)
            infos.append(SpeakerInfo(
                id=speaker_id,
                display_name=speaker_map[speaker_id],
                total_duration=0.0,
                segment_count=0,
                sample_texts=[],
                color_index=color_idx,
            ))

        if infos:
            self._show_speaker_panel(infos)

    # ------------------------------------------------------------------
    # Transcript display
    # ------------------------------------------------------------------

    def _display_segments(self, segments: list) -> None:
        """Render transcript segments in the text widget.

        Dynamically assigns colors from the SPEAKER_COLORS palette
        based on order of first appearance for non-"You" speakers.
        Places text marks at segment boundaries for edit mode.
        """
        self._text_widget.configure(state=tk.NORMAL)
        self._text_widget.delete("1.0", tk.END)

        # Store segments for edit mode
        self._displayed_segments = [
            TranscriptSegment(
                speaker=s.speaker, text=s.text,
                start_time=s.start_time, end_time=s.end_time,
            )
            for s in segments
        ]

        # Build dynamic speaker -> color mapping by order of appearance
        speaker_color_map: dict[str, str] = {"You": SPEAKER_COLORS[0]}
        next_color_idx = 1  # index 0 is for "You"

        for seg in segments:
            if seg.speaker not in speaker_color_map:
                color = SPEAKER_COLORS[next_color_idx % len(SPEAKER_COLORS)]
                speaker_color_map[seg.speaker] = color
                next_color_idx += 1

        # Create text tags for each speaker
        for speaker, color in speaker_color_map.items():
            tag_name = f"speaker_{speaker.replace(' ', '_').lower()}"
            self._text_widget.tag_configure(
                tag_name,
                foreground=color,
                font=(Font.family, Font.size_body, "bold"),
            )

        for i, seg in enumerate(segments):
            # Timestamp
            hours = int(seg.start_time) // 3600
            minutes = (int(seg.start_time) % 3600) // 60
            secs = int(seg.start_time) % 60
            ts = f"[{hours}:{minutes:02d}:{secs:02d}]"

            # Add spacing between segments
            if self._text_widget.index("end-1c") != "1.0":
                self._text_widget.insert(tk.END, "\n\n", "text_normal")

            # Place mark at start of segment header
            self._text_widget.mark_set(f"seg_{i}_header", tk.END)
            self._text_widget.mark_gravity(f"seg_{i}_header", tk.LEFT)

            # Colored left border character
            color = speaker_color_map.get(seg.speaker, Color.fg_primary)
            border_tag = f"border_{seg.speaker.replace(' ', '_').lower()}"
            if not self._text_widget.tag_names().__contains__(border_tag):
                self._text_widget.tag_configure(border_tag, foreground=color)
            self._text_widget.insert(tk.END, "\u2503 ", border_tag)

            self._text_widget.insert(tk.END, f"{ts} ", "timestamp")

            tag_name = f"speaker_{seg.speaker.replace(' ', '_').lower()}"
            self._text_widget.insert(tk.END, f"{seg.speaker}:\n", tag_name)

            # Place mark at start of segment text body
            self._text_widget.mark_set(f"seg_{i}_text", tk.END)
            self._text_widget.mark_gravity(f"seg_{i}_text", tk.LEFT)

            self._text_widget.insert(tk.END, seg.text, "text_normal")

        if not self._edit_mode:
            self._text_widget.configure(state=tk.DISABLED)

    def _clear_transcript(self) -> None:
        """Clear the transcript text widget."""
        self._text_widget.configure(state=tk.NORMAL)
        self._text_widget.delete("1.0", tk.END)
        self._text_widget.configure(state=tk.DISABLED)

    def _show_message(self, msg: str) -> None:
        """Show a message in the transcript area."""
        self._text_widget.configure(state=tk.NORMAL)
        self._text_widget.insert(tk.END, msg, "timestamp")
        self._text_widget.configure(state=tk.DISABLED)

    def show_error(self, msg: str) -> None:
        """Show an error message in the status label and transcript area.

        Called from the main thread after a worker thread failure to give
        the user visible feedback instead of silently resetting.
        """
        self._status_label.configure(text="Error")
        self._clear_transcript()
        self._show_message(msg)

    # ------------------------------------------------------------------
    # Edit mode
    # ------------------------------------------------------------------

    def _on_window_close(self) -> None:
        """Handle window close — prompt if editing."""
        if self._edit_mode:
            if not messagebox.askyesno(
                "Unsaved Edits",
                "Discard unsaved edits?",
                parent=self._window,
            ):
                return
            self._exit_edit_mode(save=False)
        self.hide()

    def _on_escape(self, event: tk.Event) -> None:
        """Handle Escape key — exit edit mode or close window."""
        if self._edit_mode:
            if self._displayed_segments:
                if messagebox.askyesno(
                    "Unsaved Edits",
                    "Discard unsaved edits?",
                    parent=self._window,
                ):
                    self._exit_edit_mode(save=False)
            else:
                self._exit_edit_mode(save=False)

    def _toggle_edit_mode(self) -> None:
        """Toggle between edit and view mode (Edit/Save button)."""
        if self._edit_mode:
            self._save_edits()
            self._exit_edit_mode(save=True)
        else:
            self._enter_edit_mode()

    def _enter_edit_mode(self) -> None:
        """Switch the transcript widget to editable mode."""
        if not self._displayed_segments:
            return

        self._edit_mode = True
        self._text_widget.configure(state=tk.NORMAL, cursor="xterm")
        self._edit_btn.configure(
            text="\u2714  Save", bg=Color.danger_alt,
            activebackground=Color.danger_alt_hover,
        )

        # Disable navigation and action buttons during editing
        self._retranscribe_btn.configure(state=tk.DISABLED, bg=Color.disabled)
        self._diarize_btn.configure(state=tk.DISABLED, bg=Color.disabled)
        self._version_dropdown.configure(state=tk.DISABLED)
        self._backend_dropdown.configure(state=tk.DISABLED)
        self._speaker_count_dropdown.configure(state=tk.DISABLED)
        self._token_btn.configure(state=tk.DISABLED)

        # Bind right-click context menu
        self._text_widget.bind("<Button-3>", self._on_right_click)

    def _exit_edit_mode(self, save: bool = False) -> None:
        """Switch back to read-only mode."""
        self._edit_mode = False
        self._text_widget.configure(state=tk.DISABLED, cursor="arrow")
        self._edit_btn.configure(
            text="\u270e  Edit", bg=Color.success,
            activebackground=Color.success_hover,
        )

        # Re-enable navigation and action buttons
        self._version_dropdown.configure(state="readonly")
        self._backend_dropdown.configure(state="readonly")
        self._speaker_count_dropdown.configure(state="readonly")
        self._token_btn.configure(state=tk.NORMAL)
        self._update_button_state()

        # Unbind right-click
        self._text_widget.unbind("<Button-3>")

    def _save_edits(self) -> None:
        """Parse edited text back to segments and write to file."""
        if self._selected_session is None or self._displayed_version is None:
            return

        num_segs = len(self._displayed_segments)
        updated_segments: list[TranscriptSegment] = []

        for i in range(num_segs):
            # Parse header: get speaker name from header text
            header_start = f"seg_{i}_header"
            text_start = f"seg_{i}_text"

            try:
                header_text = self._text_widget.get(header_start, text_start)
            except tk.TclError:
                continue

            # Extract speaker from header like "┃ [0:00:00] Speaker Name:\n"
            header_match = re.search(r"\]\s+(.+?):\s*$", header_text.strip())
            if header_match:
                speaker = header_match.group(1)
            else:
                speaker = self._displayed_segments[i].speaker

            # Get body text between this segment's text mark and next header (or END)
            if i + 1 < num_segs:
                text_end = f"seg_{i + 1}_header"
            else:
                text_end = tk.END

            try:
                body = self._text_widget.get(text_start, text_end).strip()
            except tk.TclError:
                body = ""

            if not body:
                continue  # Drop empty segments

            updated_segments.append(TranscriptSegment(
                speaker=speaker,
                text=body,
                start_time=self._displayed_segments[i].start_time,
                end_time=self._displayed_segments[i].end_time,
            ))

        if not updated_segments:
            messagebox.showwarning(
                "No Segments",
                "Cannot save — all segments are empty.\n"
                "Use Cancel or close the window to discard edits.",
                parent=self._window,
            )
            return

        # Read the original file header (everything through "---")
        transcript_path = self._selected_session.path / self._displayed_version
        original_header = self._extract_file_header(transcript_path)

        # Write
        save_edited_segments(transcript_path, updated_segments, original_header)

        # Refresh display
        self._displayed_segments = updated_segments
        self._display_segments(updated_segments)
        self._status_label.configure(text="Edits saved")

    def _extract_file_header(self, path: Path) -> str:
        """Extract everything from the start of a transcript file through '---'."""
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            return "---"

        lines = text.splitlines()
        header_lines: list[str] = []
        for line in lines:
            header_lines.append(line)
            if line.strip() == "---":
                break

        return "\n".join(header_lines)

    def _on_right_click(self, event: tk.Event) -> None:
        """Show context menu on right-click in edit mode."""
        if not self._edit_mode or not self._displayed_segments:
            return

        # Find which segment the click is in
        click_pos = self._text_widget.index(f"@{event.x},{event.y}")
        seg_index = self._find_segment_at_pos(click_pos)
        if seg_index is None:
            return

        # Determine if click is in the text body (not header)
        in_body = self._is_pos_in_body(click_pos, seg_index)

        menu = tk.Menu(self._text_widget, tearoff=0)

        # "Change Speaker" submenu
        speaker_menu = tk.Menu(menu, tearoff=0)
        all_speakers = list(dict.fromkeys(
            [s.speaker for s in self._displayed_segments] + ["You"]
        ))
        for speaker in all_speakers:
            speaker_menu.add_command(
                label=speaker,
                command=lambda s=speaker, i=seg_index: self._change_segment_speaker(i, s),
            )
        speaker_menu.add_separator()
        speaker_menu.add_command(
            label="New Speaker...",
            command=lambda i=seg_index: self._prompt_new_speaker(i),
        )
        menu.add_cascade(label="Change Speaker", menu=speaker_menu)

        # "Split Segment Here" — only if in text body
        if in_body:
            menu.add_command(
                label="Split Segment Here",
                command=lambda i=seg_index: self._split_segment_at_cursor(i),
            )

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _find_segment_at_pos(self, pos: str) -> Optional[int]:
        """Find the index of the segment containing the given text position."""
        num_segs = len(self._displayed_segments)
        for i in range(num_segs - 1, -1, -1):
            try:
                mark_pos = self._text_widget.index(f"seg_{i}_header")
                if self._text_widget.compare(pos, ">=", mark_pos):
                    return i
            except tk.TclError:
                continue
        return None

    def _is_pos_in_body(self, pos: str, seg_index: int) -> bool:
        """Check if position is within the text body (not the header line)."""
        try:
            text_mark = self._text_widget.index(f"seg_{seg_index}_text")
            return self._text_widget.compare(pos, ">=", text_mark)
        except tk.TclError:
            return False

    def _change_segment_speaker(self, seg_index: int, new_speaker: str) -> None:
        """Change the speaker for a segment and update the display."""
        if seg_index >= len(self._displayed_segments):
            return

        # First, sync any text edits from the widget back to _displayed_segments
        self._sync_text_to_segments()

        self._displayed_segments[seg_index].speaker = new_speaker

        # Refresh full display to rebuild marks and tags cleanly
        self._display_segments(self._displayed_segments)
        self._status_label.configure(
            text=f"Speaker changed to {new_speaker}"
        )

    def _sync_text_to_segments(self) -> None:
        """Read current text from widget back into _displayed_segments.

        Called before a full display refresh (speaker change, split) so that
        any text edits the user made are preserved.
        """
        num_segs = len(self._displayed_segments)
        for i in range(num_segs):
            text_start = f"seg_{i}_text"
            if i + 1 < num_segs:
                text_end = f"seg_{i + 1}_header"
            else:
                text_end = tk.END

            try:
                body = self._text_widget.get(text_start, text_end).strip()
            except tk.TclError:
                continue

            self._displayed_segments[i].text = body

    def _prompt_new_speaker(self, seg_index: int) -> None:
        """Prompt user for a new speaker name and apply it."""
        name = simpledialog.askstring(
            "New Speaker",
            "Enter speaker name:",
            parent=self._window,
        )
        if name and name.strip():
            self._change_segment_speaker(seg_index, name.strip())

    def _split_segment_at_cursor(self, seg_index: int) -> None:
        """Split a segment at the current cursor position."""
        if seg_index >= len(self._displayed_segments):
            return

        # Get cursor position
        cursor_pos = self._text_widget.index(tk.INSERT)
        text_start = f"seg_{seg_index}_text"

        # Verify cursor is in the body
        if not self._is_pos_in_body(cursor_pos, seg_index):
            return

        # Get text before and after cursor
        if seg_index + 1 < len(self._displayed_segments):
            text_end = f"seg_{seg_index + 1}_header"
        else:
            text_end = tk.END

        try:
            text_before = self._text_widget.get(text_start, cursor_pos).strip()
            text_after = self._text_widget.get(cursor_pos, text_end).strip()
        except tk.TclError:
            return

        if not text_before or not text_after:
            return  # Nothing to split

        # Sync all other segments' text edits before refreshing
        self._sync_text_to_segments()

        original = self._displayed_segments[seg_index]

        # Update the original segment with text before cursor
        original.text = text_before

        # Create new segment with text after cursor (same speaker and time)
        new_seg = TranscriptSegment(
            speaker=original.speaker,
            text=text_after,
            start_time=original.start_time,
            end_time=original.end_time,
        )
        self._displayed_segments.insert(seg_index + 1, new_seg)

        # Refresh the full display to rebuild marks cleanly
        self._display_segments(self._displayed_segments)
        self._status_label.configure(text=f"Split segment {seg_index + 1}")

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    def _on_retranscribe_click(self) -> None:
        """Handle the Re-transcribe / Cancel button click."""
        if self._batch_running:
            self._on_cancel_retranscribe()
            return

        if self._selected_session is None:
            return

        if not self._selected_session.has_mic_wav and not self._selected_session.loopback_files:
            self._status_label.configure(text="No audio files found")
            return

        self._progress_bar["value"] = 0
        self._status_label.configure(text="Starting...")
        self._on_retranscribe(self._selected_session.path)

    def _on_diarize_click(self) -> None:
        """Handle the Identify Speakers / Cancel button click.

        Automatically selects the best source transcript for diarization:
        prefers batch re-transcribed versions over the original real-time
        transcript, since batch produces longer, more coherent segments
        that yield better acoustic features for speaker clustering.
        """
        if self._diarize_running:
            if self._on_cancel_diarize:
                self._on_cancel_diarize()
            return

        if self._selected_session is None:
            return

        if not self._selected_session.loopback_files:
            self._status_label.configure(text="No loopback audio found")
            return

        if self._on_diarize is None:
            return

        if not self._selected_session.transcript_versions:
            self._status_label.configure(text="No transcript found")
            return

        # Extract backend key from dropdown display value
        backend = self._backend_var.get().strip()
        speaker_count_text = self._speaker_count_var.get().strip()
        num_speakers = int(speaker_count_text) if speaker_count_text.isdigit() else None

        # Validate pyannote backend
        if backend == "pyannote":
            if not self._validate_pyannote():
                return

        # Pick the best source transcript for diarization.
        # Prefer the latest batch (non-diarized) version over original.
        filename = self._pick_best_diarize_source()
        if not filename:
            self._status_label.configure(text="No suitable transcript found")
            return

        # Immediately show we're working — switch to cancel button +
        # indeterminate progress so the user sees feedback right away
        self._diarize_running = True
        self._update_button_state()
        self._progress_bar.configure(mode="indeterminate")
        self._progress_bar.start(15)
        self._status_label.configure(text="Starting diarization...")

        # Force the UI to repaint before handing off to the callback
        self._window.update_idletasks()

        self._on_diarize(
            self._selected_session.path, filename, backend, self._hf_token, num_speakers,
        )

    def _validate_pyannote(self) -> bool:
        """Validate pyannote.audio is installed and HF token is available.

        Returns True if validation passes, False otherwise.
        If not installed, offers to install via pip in a background thread.
        """
        import importlib.util
        try:
            pyannote_found = importlib.util.find_spec("pyannote.audio") is not None
        except (ModuleNotFoundError, ValueError):
            pyannote_found = False
        if not pyannote_found:
            answer = messagebox.askyesno(
                "Install pyannote?",
                "pyannote.audio is not installed.\n\n"
                "Would you like to install it now?\n"
                "This may take a few minutes.",
                parent=self._window,
            )
            if answer:
                self._install_pyannote()
            return False

        # Check if we have an HF token
        if not self._hf_token:
            token = simpledialog.askstring(
                "HuggingFace Token Required",
                "pyannote requires a HuggingFace token.\n\n"
                "1. Create an account at huggingface.co\n"
                "2. Accept model terms at:\n"
                "   huggingface.co/pyannote/embedding\n"
                "3. Generate a token at:\n"
                "   huggingface.co/settings/tokens\n\n"
                "Enter your HuggingFace token:",
                parent=self._window,
            )
            if not token or not token.strip():
                return False
            self._hf_token = token.strip()

        return True

    def _install_pyannote(self) -> None:
        """Run ``pip install pyannote.audio`` in a background thread."""
        import subprocess
        import sys
        import threading

        self._installing_package = True
        self._update_button_state()
        self._progress_bar.configure(mode="indeterminate")
        self._progress_bar.start(15)
        self._status_label.configure(text="Installing pyannote.audio...")

        def _run() -> None:
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "pyannote.audio"],
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                success = result.returncode == 0
                error_msg = result.stderr.strip() if not success else ""
            except subprocess.TimeoutExpired:
                success = False
                error_msg = "Installation timed out after 10 minutes."
            except Exception as exc:
                success = False
                error_msg = str(exc)

            self._root.after(0, lambda: self._on_install_complete(success, error_msg))

        threading.Thread(target=_run, daemon=True).start()

    def _on_install_complete(self, success: bool, error_msg: str) -> None:
        """Handle pip install completion on the main thread."""
        self._installing_package = False
        self._progress_bar.stop()
        self._progress_bar.configure(mode="determinate")
        self._progress_bar["value"] = 0
        self._update_button_state()

        if success:
            self._status_label.configure(text="pyannote.audio installed")
            messagebox.showinfo(
                "Installation Complete",
                "pyannote.audio has been installed successfully.\n\n"
                "Click 'Identify Speakers' again to proceed.",
                parent=self._window,
            )
        else:
            self._status_label.configure(text="Installation failed")
            messagebox.showerror(
                "Installation Failed",
                f"Failed to install pyannote.audio.\n\n{error_msg[:500]}",
                parent=self._window,
            )

    # ------------------------------------------------------------------
    # HF Token management
    # ------------------------------------------------------------------

    def _is_pyannote_selected(self) -> bool:
        """Check if the pyannote backend is currently selected."""
        if hasattr(self, "_backend_var"):
            return self._backend_var.get().strip() == "pyannote"
        return self._initial_backend == "pyannote"

    def _on_backend_changed(self) -> None:
        """Handle backend dropdown selection change — show/hide token button."""
        self._update_token_btn_visibility()

    def _token_button_text(self) -> str:
        """Return display text for the HF token button."""
        if self._hf_token:
            return "HF Saved"
        return "HF Token"

    def _update_token_btn(self) -> None:
        """Refresh the token button text and visibility."""
        if hasattr(self, "_token_btn"):
            self._token_btn.configure(text=self._token_button_text())
            self._update_token_btn_visibility()

    def _update_token_btn_visibility(self) -> None:
        """Show the token button only when pyannote is selected."""
        if not hasattr(self, "_token_btn"):
            return
        if self._is_pyannote_selected():
            self._token_btn.pack(
                side=tk.RIGHT, padx=(6, 0), pady=7,
                after=self._speaker_count_dropdown,
            )
        else:
            self._token_btn.pack_forget()

    def _on_token_btn_click(self) -> None:
        """Open the HF token management dialog."""
        dialog = tk.Toplevel(self._window)
        dialog.title("HuggingFace Token")
        dialog.configure(bg=Color.bg_primary)
        dialog.resizable(False, False)
        dialog.transient(self._window)
        dialog.grab_set()

        # Center on parent
        dialog.update_idletasks()
        w, h = 420, 200
        px = self._window.winfo_x() + (self._window.winfo_width() - w) // 2
        py = self._window.winfo_y() + (self._window.winfo_height() - h) // 2
        dialog.geometry(f"{w}x{h}+{px}+{py}")

        # Description
        tk.Label(
            dialog,
            text="HuggingFace token for pyannote speaker embeddings.",
            font=(Font.family, Font.size_small),
            fg=Color.fg_secondary, bg=Color.bg_primary,
            wraplength=380,
        ).pack(padx=16, pady=(12, 4), anchor=tk.W)

        tk.Label(
            dialog,
            text="Accept model terms at huggingface.co/pyannote/embedding",
            font=(Font.family, Font.size_tiny),
            fg=Color.fg_dim, bg=Color.bg_primary,
        ).pack(padx=16, pady=(0, 8), anchor=tk.W)

        # Token entry
        entry_frame = tk.Frame(dialog, bg=Color.bg_primary)
        entry_frame.pack(fill=tk.X, padx=16, pady=(0, 12))

        tk.Label(
            entry_frame, text="Token:",
            font=(Font.family, Font.size_body),
            fg=Color.fg_primary, bg=Color.bg_primary,
        ).pack(side=tk.LEFT, padx=(0, 6))

        token_var = tk.StringVar(value=self._hf_token)
        token_entry = tk.Entry(
            entry_frame,
            textvariable=token_var,
            font=(Font.family, Font.size_body),
            fg=Color.fg_primary, bg=Color.bg_input,
            insertbackground=Color.fg_primary,
            relief=tk.FLAT,
            show="*",
        )
        token_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Toggle visibility
        show_var = tk.BooleanVar(value=False)

        def _toggle_show() -> None:
            token_entry.configure(show="" if show_var.get() else "*")

        tk.Checkbutton(
            entry_frame, text="Show",
            variable=show_var, command=_toggle_show,
            font=(Font.family, Font.size_tiny),
            fg=Color.fg_primary, bg=Color.bg_primary,
            selectcolor=Color.bg_input,
            activebackground=Color.bg_primary,
            activeforeground=Color.fg_primary,
        ).pack(side=tk.LEFT, padx=(6, 0))

        # Buttons
        btn_frame = tk.Frame(dialog, bg=Color.bg_primary)
        btn_frame.pack(fill=tk.X, padx=16, pady=(0, 12))

        def _save() -> None:
            new_token = token_var.get().strip()
            self._hf_token = new_token
            self._update_token_btn()
            if self._on_hf_token_changed:
                self._on_hf_token_changed(new_token)
            dialog.destroy()

        def _delete() -> None:
            self._hf_token = ""
            self._update_token_btn()
            if self._on_hf_token_changed:
                self._on_hf_token_changed("")
            dialog.destroy()

        tk.Button(
            btn_frame, text="Save",
            font=(Font.family, Font.size_body),
            fg=Color.fg_bright, bg=Color.accent,
            activebackground=Color.accent_hover,
            activeforeground=Color.fg_bright,
            relief=tk.FLAT, padx=16, cursor="hand2",
            command=_save,
        ).pack(side=tk.LEFT, padx=(0, 8))

        tk.Button(
            btn_frame, text="Delete Token",
            font=(Font.family, Font.size_body),
            fg=Color.fg_bright, bg=Color.danger,
            activebackground=Color.danger_hover,
            activeforeground=Color.fg_bright,
            relief=tk.FLAT, padx=16, cursor="hand2",
            command=_delete,
        ).pack(side=tk.LEFT, padx=(0, 8))

        tk.Button(
            btn_frame, text="Cancel",
            font=(Font.family, Font.size_body),
            fg=Color.fg_bright, bg=Color.btn_neutral,
            activebackground=Color.btn_neutral_hover,
            activeforeground=Color.fg_bright,
            relief=tk.FLAT, padx=16, cursor="hand2",
            command=dialog.destroy,
        ).pack(side=tk.LEFT)

    def set_backend_config(self, backend: str, hf_token: str) -> None:
        """Set the initial backend and HF token from app config."""
        self._initial_backend = backend
        self._hf_token = hf_token
        # Update dropdown if window already exists
        if hasattr(self, "_backend_var"):
            resolved_backend = backend if backend in _BACKEND_OPTIONS else "speechbrain"
            self._backend_var.set(resolved_backend)
        self._update_token_btn()

    def _pick_best_diarize_source(self) -> Optional[str]:
        """Find the best transcript to use as diarization source.

        Prefers the latest batch (non-diarized) version.  Falls back to
        the currently selected version, or the original real-time transcript.
        Shows a status hint if only the real-time version is available.
        """
        if self._selected_session is None:
            return None

        versions = self._selected_session.transcript_versions
        session_path = self._selected_session.path

        # Find the latest batch (non-diarized) version
        best_batch: Optional[str] = None
        for v in reversed(versions):
            if v == "transcript.md":
                continue
            path = session_path / v
            if path.exists():
                try:
                    head = path.read_text(encoding="utf-8")[:500]
                    if "Diarized" not in head:
                        best_batch = v
                        break
                except Exception:
                    pass

        if best_batch:
            logger.info("Diarize: using batch transcript %s", best_batch)
            return best_batch

        # No batch version — use whatever is selected, warn user
        display = self._version_var.get()
        filename = self._display_name_to_filename(display)

        if filename == "transcript.md" or filename is None:
            self._status_label.configure(
                text="Tip: Re-transcribe first for better speaker detection"
            )
            # Still proceed with the original
            return filename or "transcript.md"

        return filename

    def _on_open_folder(self) -> None:
        """Open the selected session folder in Explorer."""
        if self._selected_session is not None:
            os.startfile(self._selected_session.path)
        else:
            os.startfile(self._output_dir)

    def _on_help_click(self) -> None:
        """Open the reviewer tutorial from the toolbar."""
        self._open_tutorial(force=True)

    # ------------------------------------------------------------------
    # Reviewer tutorial
    # ------------------------------------------------------------------

    def _open_tutorial(self, force: bool = False) -> None:
        """Open the tutorial overlay."""
        if self._window is None or not self._window.winfo_exists():
            return
        if not force and not self._tutorial_show_on_open:
            return
        if self._tutorial_window is not None and self._tutorial_window.winfo_exists():
            self._tutorial_window.lift()
            return

        self._tutorial_step_idx = 0
        self._window.update_idletasks()
        review_x = self._window.winfo_rootx()
        review_y = self._window.winfo_rooty()
        review_w = self._window.winfo_width()
        review_h = self._window.winfo_height()

        backdrop = tk.Toplevel(self._window)
        self._tutorial_backdrop = backdrop
        backdrop.overrideredirect(True)
        backdrop.transient(self._window)
        backdrop.configure(bg=Color.bg_primary)
        backdrop.geometry(f"{review_w}x{review_h}+{review_x}+{review_y}")
        try:
            backdrop.attributes("-alpha", 0.45)
        except tk.TclError:
            pass
        backdrop.bind("<Button-1>", lambda _e: self._close_tutorial())

        win = tk.Toplevel(self._window)
        self._tutorial_window = win
        win.title("Reviewer Tutorial")
        win.configure(bg=Color.bg_primary)
        win.transient(self._window)
        win.overrideredirect(True)
        win.resizable(False, False)
        card_w = 680
        card_h = 420
        card_x = review_x + max(0, (review_w - card_w) // 2)
        card_y = review_y + max(0, (review_h - card_h) // 2)
        win.geometry(f"{card_w}x{card_h}+{card_x}+{card_y}")
        win.protocol("WM_DELETE_WINDOW", self._close_tutorial)
        win.bind("<Escape>", lambda _e: self._close_tutorial())

        # Keep the walkthrough focused while still allowing easy dismissal.
        try:
            win.grab_set()
        except tk.TclError:
            pass

        container = tk.Frame(
            win,
            bg=Color.bg_primary,
            padx=16,
            pady=14,
            highlightbackground=Color.border,
            highlightthickness=1,
        )
        container.pack(fill=tk.BOTH, expand=True)

        top_row = tk.Frame(container, bg=Color.bg_primary)
        top_row.pack(fill=tk.X)

        self._tutorial_counter_label = tk.Label(
            top_row,
            text="Step 1 of 1",
            font=(Font.family, Font.size_small),
            fg=Color.fg_secondary,
            bg=Color.bg_primary,
            anchor=tk.W,
        )
        self._tutorial_counter_label.pack(side=tk.LEFT)

        close_btn = tk.Button(
            top_row,
            text="X",
            font=(Font.family, Font.size_small, "bold"),
            fg=Color.fg_primary,
            bg=Color.btn_neutral,
            activebackground=Color.btn_neutral_hover,
            activeforeground=Color.fg_bright,
            relief=tk.FLAT,
            padx=10,
            cursor="hand2",
            command=self._close_tutorial,
        )
        close_btn.pack(side=tk.RIGHT)

        tk.Frame(container, bg=Color.divider, height=1).pack(fill=tk.X, pady=(8, 12))

        self._tutorial_title_label = tk.Label(
            container,
            text="",
            font=(Font.family, Font.size_title, "bold"),
            fg=Color.fg_bright,
            bg=Color.bg_primary,
            anchor=tk.W,
            justify=tk.LEFT,
        )
        self._tutorial_title_label.pack(fill=tk.X)

        self._tutorial_body_label = tk.Label(
            container,
            text="",
            font=(Font.family, Font.size_body),
            fg=Color.fg_primary,
            bg=Color.bg_primary,
            wraplength=590,
            justify=tk.LEFT,
            anchor=tk.NW,
            padx=4,
            pady=8,
        )
        self._tutorial_body_label.pack(fill=tk.BOTH, expand=True)

        self._tutorial_show_var.set(self._tutorial_show_on_open)
        show_toggle = tk.Checkbutton(
            container,
            text="Show this walkthrough when I open Review",
            variable=self._tutorial_show_var,
            command=self._on_tutorial_toggle,
            font=(Font.family, Font.size_small),
            fg=Color.fg_secondary,
            bg=Color.bg_primary,
            activeforeground=Color.fg_primary,
            activebackground=Color.bg_primary,
            selectcolor=Color.bg_input,
            anchor=tk.W,
        )
        show_toggle.pack(fill=tk.X, pady=(2, 10))

        btn_row = tk.Frame(container, bg=Color.bg_primary)
        btn_row.pack(fill=tk.X)

        self._tutorial_back_btn = tk.Button(
            btn_row,
            text="Back",
            font=(Font.family, Font.size_small),
            fg=Color.fg_primary,
            bg=Color.btn_neutral,
            activebackground=Color.btn_neutral_hover,
            activeforeground=Color.fg_bright,
            relief=tk.FLAT,
            padx=12,
            cursor="hand2",
            command=self._on_tutorial_back,
        )
        self._tutorial_back_btn.pack(side=tk.LEFT)

        self._tutorial_next_btn = tk.Button(
            btn_row,
            text="Next",
            font=(Font.family, Font.size_small, "bold"),
            fg=Color.fg_bright,
            bg=Color.accent,
            activebackground=Color.accent_hover,
            activeforeground=Color.fg_bright,
            relief=tk.FLAT,
            padx=12,
            cursor="hand2",
            command=self._on_tutorial_next,
        )
        self._tutorial_next_btn.pack(side=tk.RIGHT, padx=(8, 0))

        self._tutorial_done_btn = tk.Button(
            btn_row,
            text="Done",
            font=(Font.family, Font.size_small, "bold"),
            fg=Color.fg_bright,
            bg=Color.success,
            activebackground=Color.success_hover,
            activeforeground=Color.fg_bright,
            relief=tk.FLAT,
            padx=12,
            cursor="hand2",
            command=self._close_tutorial,
        )
        self._tutorial_done_btn.pack(side=tk.RIGHT)

        self._render_tutorial_step()
        backdrop.lift(self._window)
        win.lift(backdrop)

    def _render_tutorial_step(self) -> None:
        """Render the current tutorial step."""
        if self._tutorial_window is None or not self._tutorial_window.winfo_exists():
            return
        total = len(_REVIEWER_TUTORIAL_STEPS)
        self._tutorial_step_idx = max(0, min(self._tutorial_step_idx, total - 1))
        title, body = _REVIEWER_TUTORIAL_STEPS[self._tutorial_step_idx]

        self._tutorial_counter_label.configure(
            text=f"Step {self._tutorial_step_idx + 1} of {total}"
        )
        self._tutorial_title_label.configure(text=title)
        self._tutorial_body_label.configure(text=body)
        self._tutorial_back_btn.configure(
            state=tk.NORMAL if self._tutorial_step_idx > 0 else tk.DISABLED
        )
        is_last = self._tutorial_step_idx == total - 1
        self._tutorial_next_btn.configure(
            state=tk.DISABLED if is_last else tk.NORMAL
        )
        self._tutorial_done_btn.configure(
            bg=Color.accent if is_last else Color.success,
            activebackground=Color.accent_hover if is_last else Color.success_hover,
        )

    def _on_tutorial_back(self) -> None:
        self._tutorial_step_idx -= 1
        self._render_tutorial_step()

    def _on_tutorial_next(self) -> None:
        self._tutorial_step_idx += 1
        self._render_tutorial_step()

    def _on_tutorial_toggle(self) -> None:
        self._set_tutorial_show_on_open(self._tutorial_show_var.get())

    def _set_tutorial_show_on_open(self, show_on_open: bool) -> None:
        """Persist the tutorial preference when changed."""
        if self._tutorial_show_on_open == show_on_open:
            return
        self._tutorial_show_on_open = show_on_open
        if self._on_tutorial_preference_changed:
            self._on_tutorial_preference_changed(show_on_open)

    def _close_tutorial(self) -> None:
        """Close tutorial overlay if open."""
        if self._tutorial_backdrop is not None and self._tutorial_backdrop.winfo_exists():
            self._tutorial_backdrop.destroy()
        self._tutorial_backdrop = None

        if self._tutorial_window is None:
            return
        if self._tutorial_window.winfo_exists():
            try:
                self._tutorial_window.grab_release()
            except tk.TclError:
                pass
            self._tutorial_window.destroy()
        self._tutorial_window = None

    # ------------------------------------------------------------------
    # Button state management
    # ------------------------------------------------------------------

    def _update_button_state(self) -> None:
        """Enable/disable buttons based on current state."""
        if not hasattr(self, "_retranscribe_btn"):
            return

        any_running = self._batch_running or self._diarize_running or self._installing_package

        # -- Re-transcribe button --
        if self._batch_running:
            self._retranscribe_btn.configure(
                text="\u2715  Cancel",
                bg=Color.danger,
                activebackground=Color.danger_hover,
                state=tk.NORMAL,
            )
        elif self._recording_active or self._diarize_running or self._installing_package:
            self._retranscribe_btn.configure(
                text="\u21bb  Re-transcribe",
                bg=Color.disabled,
                state=tk.DISABLED,
            )
        elif self._selected_session is None:
            self._retranscribe_btn.configure(
                text="\u21bb  Re-transcribe",
                bg=Color.disabled,
                state=tk.DISABLED,
            )
        elif not self._selected_session.has_mic_wav and not self._selected_session.loopback_files:
            self._retranscribe_btn.configure(
                text="\u21bb  Re-transcribe",
                bg=Color.disabled,
                state=tk.DISABLED,
            )
        else:
            self._retranscribe_btn.configure(
                text="\u21bb  Re-transcribe",
                bg=Color.accent,
                activebackground=Color.accent_hover,
                state=tk.NORMAL,
            )

        # -- Identify Speakers button --
        if not hasattr(self, "_diarize_btn"):
            return

        if self._diarize_running:
            self._diarize_btn.configure(
                text="\u2715  Cancel",
                bg=Color.danger,
                activebackground=Color.danger_hover,
                state=tk.NORMAL,
            )
        elif self._recording_active or self._batch_running or self._installing_package:
            self._diarize_btn.configure(
                text="\U0001f5e3  Identify Speakers",
                bg=Color.disabled,
                state=tk.DISABLED,
            )
        elif self._selected_session is None:
            self._diarize_btn.configure(
                text="\U0001f5e3  Identify Speakers",
                bg=Color.disabled,
                state=tk.DISABLED,
            )
        elif not self._selected_session.loopback_files:
            self._diarize_btn.configure(
                text="\U0001f5e3  Identify Speakers",
                bg=Color.disabled,
                state=tk.DISABLED,
            )
        elif not self._selected_session.transcript_versions:
            self._diarize_btn.configure(
                text="\U0001f5e3  Identify Speakers",
                bg=Color.disabled,
                state=tk.DISABLED,
            )
        else:
            self._diarize_btn.configure(
                text="\U0001f5e3  Identify Speakers",
                bg=Color.purple,
                activebackground=Color.purple_hover,
                state=tk.NORMAL,
            )

        # -- Edit button --
        if not hasattr(self, "_edit_btn"):
            return

        if any_running or self._selected_session is None or not self._displayed_segments:
            self._edit_btn.configure(
                text="\u270e  Edit", bg=Color.disabled, state=tk.DISABLED,
            )
        elif not self._edit_mode:
            self._edit_btn.configure(
                text="\u270e  Edit", bg=Color.success,
                activebackground=Color.success_hover,
                state=tk.NORMAL,
            )
        # When in edit mode, button state is managed by _enter/_exit_edit_mode

        control_state = tk.DISABLED if any_running or self._edit_mode else "readonly"
        self._backend_dropdown.configure(state=control_state)
        self._speaker_count_dropdown.configure(state=control_state)
        self._token_btn.configure(
            state=tk.DISABLED if any_running or self._edit_mode else tk.NORMAL
        )

    # ------------------------------------------------------------------
    # Window geometry persistence
    # ------------------------------------------------------------------

    def _save_window_geometry(self) -> None:
        """Save the current window geometry via callback."""
        if self._window is None or not self._window.winfo_exists():
            return
        if self._on_save_geometry:
            geo = self._window.geometry()
            self._on_save_geometry(geo)

    @staticmethod
    def _is_geometry_on_screen(geometry: str) -> bool:
        """Check if a geometry string places the window reasonably on-screen."""
        m = re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", geometry)
        if not m:
            return False
        w, h, x, y = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        # Reject if window is entirely off-screen (generous bounds)
        if x < -w or y < -h or x > 5000 or y > 3000:
            return False
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _version_display_name(filename: str) -> str:
        """Convert a transcript filename to a display name.

        ``transcript.md`` -> ``Original (real-time)``
        ``transcript_v2.md`` -> ``v2 (batch)``

        Note: Diarized versions are detected by reading the file header.
        For efficiency, we check the version line when loading.
        """
        if filename == "transcript.md":
            return "Original (real-time)"
        m = re.search(r"_v(\d+)\.md$", filename)
        if m:
            return f"v{m.group(1)} (batch)"
        return filename

    def _version_display_name_with_context(self, filename: str) -> str:
        """Like _version_display_name but checks file content for type."""
        if filename == "transcript.md":
            return "Original (real-time)"

        m = re.search(r"_v(\d+)\.md$", filename)
        if not m:
            return filename

        ver = m.group(1)

        # Check file for diarized marker (with optional backend name)
        if self._selected_session is not None:
            path = self._selected_session.path / filename
            if path.exists():
                try:
                    head = path.read_text(encoding="utf-8")[:500]
                    if "Diarized" in head:
                        # Extract backend name e.g. "(Diarized — pyannote)"
                        bm = re.search(r"Diarized\s*\u2014\s*(\w+)", head)
                        if bm:
                            return f"v{ver} (diarized, {bm.group(1)})"
                        return f"v{ver} (diarized)"
                except Exception:
                    pass

        return f"v{ver} (batch)"

    @staticmethod
    def _display_name_to_filename(display: str) -> Optional[str]:
        """Convert a display name back to a filename."""
        if display == "Original (real-time)":
            return "transcript.md"
        m = re.match(r"^v(\d+)\s", display)
        if m:
            return f"transcript_v{m.group(1)}.md"
        return None

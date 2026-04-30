"""First-run setup wizard with legal/consent disclaimer.

Shown on first launch (and on demand from the tray menu).  Walks the
user through:
    1. Welcome + detected hardware summary
    2. Legal / recording-consent disclaimer (must be acknowledged)
    3. Microphone selection
    4. System audio (loopback) device selection
    5. Done — points user at the tray icon

The wizard is a modal :class:`tkinter.Toplevel` parented to the existing
overlay Tk root so it shares the tkinter main loop.  If the user closes
the wizard without completing it, ``first_run_complete`` is NOT set and
the wizard will re-appear next launch.

Consent is a hard gate — recording cannot start unless
``config.consent_acknowledged`` is ``True``.  The wizard writes it when
the user ticks the disclaimer checkbox and clicks Next on that page.
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk
from typing import Callable, List, Optional

from tachyon.config import Config
from tachyon.hardware import HardwareInfo, detect_hardware, recommend_model_size

logger = logging.getLogger(__name__)


# -- Visual constants --------------------------------------------------------

_BG_COLOR: str = "#f5f5f5"
_FG_COLOR: str = "#1a1a1a"
_ACCENT_COLOR: str = "#0066cc"
_WARNING_COLOR: str = "#a03030"
_FONT_FAMILY: str = "Segoe UI"
_TITLE_FONT: tuple = (_FONT_FAMILY, 16, "bold")
_BODY_FONT: tuple = (_FONT_FAMILY, 10)
_SMALL_FONT: tuple = (_FONT_FAMILY, 9)
_WIN_WIDTH: int = 560
_WIN_HEIGHT: int = 480


_CONSENT_TEXT: str = (
    "Recording laws vary significantly by country, state, and "
    "jurisdiction.\n\n"
    "In some places (one-party consent) recording a conversation you are "
    "part of is legal. In other places (all-party consent) you must "
    "inform every participant before recording — this includes most of "
    "Europe (GDPR), California, Florida, Illinois, Maryland, "
    "Massachusetts, Montana, New Hampshire, Pennsylvania, and Washington "
    "State.\n\n"
    "Workplace policies, employment contracts, and the terms of service "
    "of meeting platforms (Zoom, Teams, Meet, etc.) may further "
    "restrict recording even where it is legal.\n\n"
    "Tachyon records and processes audio locally on your own computer. "
    "It does not send data anywhere. Even so, YOU are responsible for "
    "complying with all applicable laws, policies, and terms of service "
    "where you use it.\n\n"
    "The developers of Tachyon accept no liability for misuse. When in "
    "doubt, ask for consent before recording."
)


class FirstRunWizard:
    """Modal first-run setup wizard."""

    def __init__(
        self,
        root: tk.Tk,
        config: Config,
        hardware_info: Optional[HardwareInfo] = None,
        mic_devices: Optional[List[dict]] = None,
        loopback_devices: Optional[List[dict]] = None,
        on_complete: Optional[Callable[[], None]] = None,
    ) -> None:
        """Create the wizard (but do not show it yet — call :meth:`run`).

        Parameters:
            root: The existing tkinter root (from the overlay).
            config: The live Config object.  Will be updated in place as
                the user makes choices.  Caller is responsible for
                calling ``config.save()`` after the wizard finishes.
            hardware_info: Pre-detected hardware info.  If ``None``,
                detection runs when the wizard is shown.
            mic_devices: Pre-enumerated list of WASAPI input devices
                (dicts with ``name`` and ``index`` keys).  ``None`` = skip
                the mic page (system default will be used).
            loopback_devices: Pre-enumerated list of WASAPI output
                devices for loopback.  ``None`` = skip the loopback page.
            on_complete: Called when the user clicks Finish on the last
                page.  Not called if the user closes the dialog early.
        """
        self._root = root
        self._config = config
        self._hw = hardware_info
        self._mic_devices = mic_devices or []
        self._loopback_devices = loopback_devices or []
        self._on_complete = on_complete

        # Lazy-initialised state
        self._win: Optional[tk.Toplevel] = None
        self._current_page: int = 0
        self._pages: list[Callable[[], None]] = [
            self._render_welcome,
            self._render_consent,
            self._render_mic,
            self._render_loopback,
            self._render_done,
        ]

        # Per-page widgets (cleared on page change)
        self._page_frame: Optional[tk.Frame] = None
        self._next_btn: Optional[tk.Button] = None
        self._back_btn: Optional[tk.Button] = None
        self._finish_btn: Optional[tk.Button] = None

        # Consent state
        self._consent_var: Optional[tk.BooleanVar] = None

        # Device selection state
        self._mic_var: Optional[tk.StringVar] = None
        self._loopback_vars: list[tk.BooleanVar] = []

        # Track whether the user completed the wizard.  Used by the
        # window-close handler to avoid setting first_run_complete if
        # they bailed mid-wizard.
        self._finished: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Show the wizard modally and block until the user finishes or closes it."""
        if self._hw is None:
            self._hw = detect_hardware()

        self._create_window()
        self._current_page = 0
        self._show_page()

        # Block this tkinter call until the window is destroyed.  The
        # overlay mainloop is still running (we're inside it via
        # root.after), so grab_set() gives us modal behavior.
        self._win.wait_window()

    def _create_window(self) -> None:
        """Build the Toplevel and its fixed outer layout."""
        win = tk.Toplevel(self._root)
        win.title("Tachyon Transcripts — Welcome")
        win.configure(bg=_BG_COLOR)
        win.geometry(f"{_WIN_WIDTH}x{_WIN_HEIGHT}")
        win.resizable(False, False)

        # Center on screen
        win.update_idletasks()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        x = (sw - _WIN_WIDTH) // 2
        y = (sh - _WIN_HEIGHT) // 2
        win.geometry(f"+{x}+{y}")

        # Make modal
        win.transient(self._root)
        try:
            win.grab_set()
        except tk.TclError:
            # grab can fail if the root isn't mapped yet — harmless
            pass

        # Close-handler: don't mark first_run_complete if user bails
        win.protocol("WM_DELETE_WINDOW", self._on_close_window)

        # -- Page container --
        self._page_frame = tk.Frame(win, bg=_BG_COLOR)
        self._page_frame.pack(
            fill="both", expand=True,
            padx=24, pady=(20, 0),
        )

        # -- Bottom button bar --
        button_bar = tk.Frame(win, bg=_BG_COLOR)
        button_bar.pack(fill="x", side="bottom", padx=24, pady=16)

        self._back_btn = tk.Button(
            button_bar, text="Back", font=_BODY_FONT,
            width=10, command=self._on_back,
        )
        self._back_btn.pack(side="left")

        self._finish_btn = tk.Button(
            button_bar, text="Finish", font=_BODY_FONT,
            width=10, command=self._on_finish,
        )
        # Finish button is hidden except on last page

        self._next_btn = tk.Button(
            button_bar, text="Next  \u25B6", font=_BODY_FONT,
            width=10, command=self._on_next,
        )
        self._next_btn.pack(side="right")

        self._win = win

    # ------------------------------------------------------------------
    # Page navigation
    # ------------------------------------------------------------------

    def _show_page(self) -> None:
        """Clear the page frame and render the current page."""
        if self._page_frame is None:
            return
        for child in self._page_frame.winfo_children():
            child.destroy()

        # Render the current page
        self._pages[self._current_page]()

        # Update button visibility / labels
        is_first = self._current_page == 0
        is_last = self._current_page == len(self._pages) - 1

        if self._back_btn is not None:
            self._back_btn.configure(
                state="normal" if not is_first else "disabled",
            )

        if self._next_btn is not None and self._finish_btn is not None:
            if is_last:
                self._next_btn.pack_forget()
                self._finish_btn.pack(side="right")
            else:
                self._finish_btn.pack_forget()
                self._next_btn.pack(side="right")

        # Page-specific gates (e.g. consent must be ticked)
        self._update_next_state()

    def _on_back(self) -> None:
        if self._current_page > 0:
            self._current_page -= 1
            self._show_page()

    def _on_next(self) -> None:
        # Validate before advancing
        if self._current_page == 1:
            # Consent page — write ack flag
            if self._consent_var is not None and self._consent_var.get():
                self._config.consent_acknowledged = True
            else:
                return  # shouldn't happen because Next is disabled
        elif self._current_page == 2:
            # Mic page — persist selection
            self._save_mic_selection()
        elif self._current_page == 3:
            # Loopback page — persist selection
            self._save_loopback_selection()

        if self._current_page < len(self._pages) - 1:
            self._current_page += 1
            self._show_page()

    def _on_finish(self) -> None:
        """User clicked Finish on the last page."""
        self._config.first_run_complete = True
        self._finished = True
        if self._on_complete is not None:
            try:
                self._on_complete()
            except Exception:
                logger.exception("on_complete callback raised")
        if self._win is not None:
            self._win.destroy()

    def _on_close_window(self) -> None:
        """User closed the window via the X button or Alt-F4."""
        # Do NOT mark first_run_complete — the wizard will reappear next launch.
        logger.info(
            "First-run wizard closed before completion — wizard will reappear.",
        )
        if self._win is not None:
            self._win.destroy()

    def _update_next_state(self) -> None:
        """Enable/disable Next based on current-page validity."""
        if self._next_btn is None:
            return
        # Only the consent page has a hard gate in v1.
        if self._current_page == 1:
            ok = self._consent_var is not None and self._consent_var.get()
            self._next_btn.configure(state="normal" if ok else "disabled")
        else:
            self._next_btn.configure(state="normal")

    # ------------------------------------------------------------------
    # Page renderers
    # ------------------------------------------------------------------

    def _render_welcome(self) -> None:
        """Page 1: Welcome + hardware summary."""
        assert self._page_frame is not None
        assert self._hw is not None

        tk.Label(
            self._page_frame,
            text="Welcome to Tachyon Transcripts",
            font=_TITLE_FONT, bg=_BG_COLOR, fg=_FG_COLOR,
        ).pack(anchor="w")

        tk.Label(
            self._page_frame,
            text=(
                "Tachyon records meetings and transcribes them locally on "
                "your computer using Whisper. Nothing is uploaded to the "
                "cloud.\n\n"
                "This quick setup will check your hardware, walk you "
                "through the legal basics of recording, and help you pick "
                "your microphone and system audio device."
            ),
            font=_BODY_FONT, bg=_BG_COLOR, fg=_FG_COLOR,
            wraplength=_WIN_WIDTH - 72, justify="left",
        ).pack(anchor="w", pady=(12, 20))

        # Hardware summary box
        hw_frame = tk.Frame(
            self._page_frame, bg="#ffffff",
            highlightthickness=1, highlightbackground="#d0d0d0",
        )
        hw_frame.pack(fill="x", pady=(0, 12))

        tk.Label(
            hw_frame, text="Detected hardware",
            font=(_FONT_FAMILY, 10, "bold"),
            bg="#ffffff", fg=_FG_COLOR,
        ).pack(anchor="w", padx=14, pady=(10, 4))

        tk.Label(
            hw_frame, text=self._hw.summary,
            font=_BODY_FONT, bg="#ffffff", fg=_FG_COLOR,
        ).pack(anchor="w", padx=14)

        suggested_model = recommend_model_size(self._hw)
        tk.Label(
            hw_frame,
            text=f"Recommended Whisper model: {suggested_model}",
            font=_SMALL_FONT, bg="#ffffff", fg="#555555",
        ).pack(anchor="w", padx=14, pady=(2, 10))

        if not self._hw.has_cuda:
            tk.Label(
                self._page_frame,
                text=(
                    "\u26A0  No NVIDIA GPU detected. Transcription will "
                    "run on CPU. Expect longer processing times than a "
                    "GPU setup — the distilled model keeps it usable."
                ),
                font=_SMALL_FONT, bg=_BG_COLOR, fg=_WARNING_COLOR,
                wraplength=_WIN_WIDTH - 72, justify="left",
            ).pack(anchor="w")

    def _render_consent(self) -> None:
        """Page 2: Recording-law disclaimer + required acknowledgement."""
        assert self._page_frame is not None

        tk.Label(
            self._page_frame,
            text="Recording & the law",
            font=_TITLE_FONT, bg=_BG_COLOR, fg=_FG_COLOR,
        ).pack(anchor="w")

        # Scrollable text area for the long legal blurb
        text_frame = tk.Frame(self._page_frame, bg=_BG_COLOR)
        text_frame.pack(fill="both", expand=True, pady=(12, 12))

        text_widget = tk.Text(
            text_frame, font=_BODY_FONT, wrap="word",
            bg="#ffffff", fg=_FG_COLOR,
            highlightthickness=1, highlightbackground="#d0d0d0",
            borderwidth=0, padx=12, pady=10, height=12,
        )
        text_widget.insert("1.0", _CONSENT_TEXT)
        text_widget.configure(state="disabled")
        text_widget.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(
            text_frame, orient="vertical", command=text_widget.yview,
        )
        scrollbar.pack(side="right", fill="y")
        text_widget.configure(yscrollcommand=scrollbar.set)

        # Consent checkbox — required
        if self._consent_var is None:
            self._consent_var = tk.BooleanVar(
                value=self._config.consent_acknowledged,
            )
        tk.Checkbutton(
            self._page_frame,
            text=(
                "I understand recording laws vary by jurisdiction and I "
                "am responsible for complying with them."
            ),
            variable=self._consent_var,
            font=(_FONT_FAMILY, 10, "bold"),
            bg=_BG_COLOR, fg=_FG_COLOR,
            activebackground=_BG_COLOR, selectcolor="#ffffff",
            wraplength=_WIN_WIDTH - 72, justify="left", anchor="w",
            command=self._update_next_state,
        ).pack(anchor="w")

    def _render_mic(self) -> None:
        """Page 3: Microphone selection."""
        assert self._page_frame is not None

        tk.Label(
            self._page_frame, text="Choose your microphone",
            font=_TITLE_FONT, bg=_BG_COLOR, fg=_FG_COLOR,
        ).pack(anchor="w")

        tk.Label(
            self._page_frame,
            text=(
                "Pick the mic Tachyon will use for your own voice. You "
                "can change this later from the tray menu."
            ),
            font=_BODY_FONT, bg=_BG_COLOR, fg=_FG_COLOR,
            wraplength=_WIN_WIDTH - 72, justify="left",
        ).pack(anchor="w", pady=(12, 16))

        # Filter to WASAPI input devices only
        inputs: list[dict] = []
        for d in self._mic_devices:
            if d.get("max_input_channels", 0) > 0:
                inputs.append(d)

        options: list[str] = ["System default"]
        for d in inputs:
            options.append(d.get("name", "(unknown)"))

        initial = self._config.mic_device or "System default"
        if initial not in options:
            options.insert(1, initial)  # keep whatever they had

        if self._mic_var is None:
            self._mic_var = tk.StringVar(value=initial)

        if not inputs:
            tk.Label(
                self._page_frame,
                text=(
                    "\u26A0  No WASAPI input devices were detected. "
                    "Tachyon will use the Windows default input. "
                    "You can change this later from the tray menu."
                ),
                font=_SMALL_FONT, bg=_BG_COLOR, fg=_WARNING_COLOR,
                wraplength=_WIN_WIDTH - 72, justify="left",
            ).pack(anchor="w")
            return

        combo = ttk.Combobox(
            self._page_frame, textvariable=self._mic_var,
            values=options, state="readonly", font=_BODY_FONT, width=50,
        )
        combo.pack(anchor="w")

    def _render_loopback(self) -> None:
        """Page 4: System-audio loopback device selection."""
        assert self._page_frame is not None

        tk.Label(
            self._page_frame, text="Choose your system audio source",
            font=_TITLE_FONT, bg=_BG_COLOR, fg=_FG_COLOR,
        ).pack(anchor="w")

        tk.Label(
            self._page_frame,
            text=(
                "Tachyon records whatever is playing through the output "
                "devices you select here (Zoom, Teams, YouTube, etc.). "
                "Leave blank to use the Windows default output, or tick "
                "multiple devices if you run chat & game audio through "
                "separate outputs."
            ),
            font=_BODY_FONT, bg=_BG_COLOR, fg=_FG_COLOR,
            wraplength=_WIN_WIDTH - 72, justify="left",
        ).pack(anchor="w", pady=(12, 8))

        tk.Label(
            self._page_frame,
            text=(
                "⚠  Heads-up: while recording, Tachyon captures every "
                "sound that comes out of the selected output devices "
                "— including notifications, other apps, and anything "
                "spoken aloud near the mic. Recordings are saved to disk "
                "as unencrypted WAV files in your output folder."
            ),
            font=_SMALL_FONT, bg=_BG_COLOR, fg=_WARNING_COLOR,
            wraplength=_WIN_WIDTH - 72, justify="left",
        ).pack(anchor="w", pady=(0, 16))

        # Build initial check state from existing config
        enabled_names = {
            d.get("device_name", "") for d in self._config.loopback_devices
            if d.get("enabled", True) and d.get("device_name")
        }

        if not self._loopback_devices:
            tk.Label(
                self._page_frame,
                text=(
                    "\u26A0  No WASAPI output devices detected. Tachyon "
                    "will use the Windows default output. You can "
                    "adjust this later from the tray menu."
                ),
                font=_SMALL_FONT, bg=_BG_COLOR, fg=_WARNING_COLOR,
                wraplength=_WIN_WIDTH - 72, justify="left",
            ).pack(anchor="w")
            return

        # Reset list each render so it matches the checkbox widgets
        self._loopback_vars = []
        scroll_frame = tk.Frame(self._page_frame, bg=_BG_COLOR)
        scroll_frame.pack(fill="both", expand=True)

        for dev in self._loopback_devices:
            name = dev.get("name", "(unknown)")
            var = tk.BooleanVar(value=name in enabled_names)
            self._loopback_vars.append(var)
            tk.Checkbutton(
                scroll_frame, text=name, variable=var,
                font=_BODY_FONT, bg=_BG_COLOR, fg=_FG_COLOR,
                activebackground=_BG_COLOR, selectcolor="#ffffff",
                anchor="w", wraplength=_WIN_WIDTH - 90, justify="left",
            ).pack(anchor="w", pady=2)

    def _render_done(self) -> None:
        """Page 5: All set."""
        assert self._page_frame is not None

        tk.Label(
            self._page_frame, text="You're all set",
            font=_TITLE_FONT, bg=_BG_COLOR, fg=_FG_COLOR,
        ).pack(anchor="w")

        tk.Label(
            self._page_frame,
            text=(
                "Tachyon lives in your system tray — look for its icon "
                "near the clock.\n\n"
                "To start recording:\n"
                "   Right-click the tray icon\n"
                "   and choose Start Recording.\n\n"
                "To show or hide live captions, press "
                f"{self._config.hotkey.upper()} or use the tray menu.\n\n"
                "Recordings are saved as Markdown transcripts plus the "
                "original audio in the output folder (also configurable "
                "from the tray menu).\n\n"
                "You can re-open this wizard anytime from the tray menu "
                "under \u201CSetup Wizard\u201D."
            ),
            font=_BODY_FONT, bg=_BG_COLOR, fg=_FG_COLOR,
            wraplength=_WIN_WIDTH - 72, justify="left",
        ).pack(anchor="w", pady=(12, 0))

    # ------------------------------------------------------------------
    # Save handlers
    # ------------------------------------------------------------------

    def _save_mic_selection(self) -> None:
        """Persist the chosen mic to config."""
        if self._mic_var is None:
            return
        value = self._mic_var.get()
        if value == "System default":
            self._config.mic_device = None
        else:
            self._config.mic_device = value

    def _save_loopback_selection(self) -> None:
        """Persist the chosen loopback devices to config."""
        if not self._loopback_vars:
            return

        selected: list[dict] = []
        for var, dev in zip(self._loopback_vars, self._loopback_devices):
            if var.get():
                name = dev.get("name", "")
                if name:
                    selected.append({
                        "device_name": name,
                        "label": _extract_short_label(name),
                        "enabled": True,
                    })
        self._config.loopback_devices = selected


def _extract_short_label(device_name: str) -> str:
    """Extract a short label from a device name, e.g. 'Chat' from
    'Headset Earphone (Arctis 7 Chat)'.  Duplicated from main.py so the
    wizard doesn't depend on the App class.
    """
    import re
    m = re.search(r"\((.+?)\)", device_name)
    if m:
        parts = m.group(1).strip().split()
        if parts:
            return parts[-1]
    parts = device_name.split()
    return parts[0] if parts else device_name

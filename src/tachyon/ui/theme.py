"""Centralised visual theme for Tachyon Transcripts UI.

All colours, fonts, and dimensions used by the overlay, reviewer, and tray
components are defined here.  Modules import from this file instead of
scattering hex codes and magic numbers across individual files.

Future extensions (light mode, user-customisable themes) only need to
touch this single file.
"""

from __future__ import annotations

import tkinter as tk
from typing import Optional


# -- Colours ------------------------------------------------------------------

class Color:
    """Semantic colour tokens for the dark sci-fi theme."""

    # Backgrounds -- ordered from darkest to lightest (deep blue-tinted)
    bg_primary: str = "#0a0e17"       # Main window / transcript area
    bg_secondary: str = "#0f1520"     # Sidebar / panels
    bg_surface: str = "#111927"       # Transcript text area (subtle lift)
    bg_elevated: str = "#151d2e"      # Toolbars, headers, bottom bars
    bg_input: str = "#1a2436"         # Text entry fields

    # Foreground / text (cool-tinted)
    fg_primary: str = "#c8d6e5"       # Default body text
    fg_bright: str = "#ffffff"        # Button text, overlay captions
    fg_secondary: str = "#6b8299"     # Muted labels, status text
    fg_muted: str = "#4a6580"         # Timestamps, tertiary info
    fg_dim: str = "#3d5570"           # Least prominent text

    # Accent -- cyan (primary actions, selection)
    accent: str = "#00b4d8"
    accent_hover: str = "#00d4ff"

    # Success -- teal-green (edit button)
    success: str = "#00cc88"
    success_hover: str = "#00e699"

    # Danger -- red (cancel, destructive)
    danger: str = "#c42b1c"
    danger_hover: str = "#d63b2b"
    danger_alt: str = "#c44444"       # Edit-mode save (red to signal "active")
    danger_alt_hover: str = "#d55555"

    # Purple (diarisation)
    purple: str = "#8b5cf6"
    purple_hover: str = "#a78bfa"

    # Speaker colours (index 0 is reserved for "You")
    speaker_you: str = "#66b3ff"
    speaker_them: str = "#ff9966"
    speaker_panel_bg: str = "#0d1926"

    # Timestamp
    timestamp: str = "#4a6580"

    # Selection / active states
    selection: str = "#0a3d6b"

    # Borders & dividers
    border: str = "#1e3050"
    border_subtle: str = "#162440"

    # Neutral buttons (secondary actions)
    btn_neutral: str = "#1a2436"
    btn_neutral_hover: str = "#243350"

    # Disabled state
    disabled: str = "#3d5570"

    # Overlay-specific
    overlay_bg: str = "#080c14"
    overlay_titlebar: str = "#060a12"
    overlay_btn_hover: str = "#1a2436"

    # Progress bar
    progress_trough: str = "#162030"

    # Scrollbar
    scrollbar_trough: str = "#0e1520"

    # Recording indicator
    recording_dot: str = "#dc1e1e"

    # Glow colours (for custom widgets)
    glow_primary: str = "#00b4d8"
    glow_secondary: str = "#0077b6"
    glow_success: str = "#00cc88"
    glow_danger: str = "#ff4444"

    # Session card states
    bg_card: str = "#121c2d"
    bg_card_hover: str = "#182840"
    bg_card_selected: str = "#0a3d6b"
    border_glow: str = "#00b4d833"    # Semi-transparent for layered glow

    # Divider
    divider: str = "#1e3050"


# -- Fonts --------------------------------------------------------------------

class Font:
    """Font family and standard sizes."""

    family: str = "Segoe UI"
    family_mono: str = "Cascadia Code"  # Fallback: Consolas
    _mono_checked: bool = False
    _mono_available: bool = True

    # Sizes
    size_body: int = 13           # Main body text
    size_small: int = 10          # Secondary labels, metadata
    size_tiny: int = 9            # Config dropdowns, fine print
    size_caption: int = 9         # Fine metadata / badges
    size_header: int = 13         # Section headers
    size_title: int = 14          # Session title, overlay captions
    size_titlebar: int = 11       # Window titlebar text
    size_btn: int = 11            # Button labels

    @classmethod
    def mono(cls) -> str:
        """Return the monospace font family, checking availability once."""
        if not cls._mono_checked:
            try:
                available = tk.Tk.tk.call("font", "families")  # noqa
            except Exception:
                available = ()
            cls._mono_available = cls.family_mono.lower() in [
                f.lower() for f in available
            ]
            if not cls._mono_available:
                cls.family_mono = "Consolas"
            cls._mono_checked = True
        return cls.family_mono


# -- Dimensions ---------------------------------------------------------------

class Dim:
    """Layout dimensions in pixels."""

    # Reviewer window
    reviewer_min_width: int = 900
    reviewer_min_height: int = 650
    reviewer_default_width: int = 1000
    reviewer_default_height: int = 700
    sidebar_default_width: int = 280
    sidebar_min_width: int = 160
    toolbar_height: int = 52

    # Overlay
    overlay_width: int = 600
    expanded_width: int = 650
    expanded_height: int = 400
    titlebar_height: int = 28
    overlay_padding_x: int = 18
    overlay_padding_y: int = 12
    overlay_bottom_margin: int = 60
    overlay_max_lines: int = 4
    overlay_poll_ms: int = 100

    # Tray icon
    icon_size: int = 64

    # Cards & spacing
    card_padding_x: int = 14
    card_padding_y: int = 10
    card_gap: int = 4
    section_padding: int = 16


# -- Speaker palette ----------------------------------------------------------
# Overlay uses a separate palette (5 colours) for non-"You" speakers.
# The reviewer imports SPEAKER_COLORS from diarizer.py (8 colours) which
# includes the "You" colour at index 0.

OVERLAY_SPEAKER_PALETTE: list[str] = [
    "#ff9966",  # orange (default for single "Them")
    "#66ff99",  # green
    "#ff66ff",  # pink
    "#ffff66",  # yellow
    "#66ffff",  # cyan
]


# -- Tooltip helper -----------------------------------------------------------

class ToolTip:
    """Lightweight tooltip that appears on hover after a short delay.

    Usage::

        btn = tk.Button(parent, text="Do thing")
        ToolTip(btn, "Explain what this does")
    """

    _DELAY_MS: int = 500
    _BG: str = Color.bg_elevated
    _FG: str = Color.fg_primary
    _BORDER: str = Color.border
    _FONT: tuple = (Font.family, Font.size_small)

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self._widget = widget
        self._text = text
        self._tip_window: Optional[tk.Toplevel] = None
        self._after_id: Optional[str] = None

        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._cancel, add="+")
        widget.bind("<ButtonPress>", self._cancel, add="+")

    def _schedule(self, event: tk.Event) -> None:
        self._cancel(event)
        self._after_id = self._widget.after(self._DELAY_MS, self._show)

    def _cancel(self, event: tk.Event) -> None:
        if self._after_id:
            self._widget.after_cancel(self._after_id)
            self._after_id = None
        self._hide()

    def _show(self) -> None:
        if self._tip_window is not None:
            return
        x = self._widget.winfo_rootx() + 20
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4

        self._tip_window = tw = tk.Toplevel(self._widget)
        tw.wm_overrideredirect(True)
        tw.wm_attributes("-topmost", True)

        label = tk.Label(
            tw, text=self._text,
            font=self._FONT,
            fg=self._FG, bg=self._BG,
            relief=tk.SOLID, borderwidth=1,
            padx=10, pady=6,
            highlightbackground=self._BORDER,
            highlightthickness=1,
            wraplength=300,
        )
        label.pack()
        tw.wm_geometry(f"+{x}+{y}")

    def _hide(self) -> None:
        if self._tip_window:
            self._tip_window.destroy()
            self._tip_window = None

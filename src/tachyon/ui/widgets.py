"""Reusable custom widgets for the Tachyon Transcripts sci-fi UI.

Provides higher-fidelity visual components than stock tkinter widgets:
  - HoverButton: Frame-based button with smooth hover transitions
  - GlowFrame: Canvas wrapper that draws a glow border around content
  - GradientBar: Canvas that renders a horizontal gradient background
  - PulseIndicator: Canvas with animated pulsing glow circle
  - SessionCard: Rich card widget for the session list sidebar
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable, Optional

from tachyon.ui.theme import Color, Font, Dim


# ---------------------------------------------------------------------------
# Colour utilities
# ---------------------------------------------------------------------------

def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Convert '#RRGGBB' to (R, G, B)."""
    h = hex_color.lstrip("#")
    if len(h) == 8:  # RRGGBBAA
        h = h[:6]
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    """Convert (R, G, B) to '#RRGGBB'."""
    return f"#{r:02x}{g:02x}{b:02x}"


def _lerp_color(c1: str, c2: str, t: float) -> str:
    """Linearly interpolate between two hex colours.  *t* in [0, 1]."""
    r1, g1, b1 = _hex_to_rgb(c1)
    r2, g2, b2 = _hex_to_rgb(c2)
    return _rgb_to_hex(
        int(r1 + (r2 - r1) * t),
        int(g1 + (g2 - g1) * t),
        int(b1 + (b2 - b1) * t),
    )


# ---------------------------------------------------------------------------
# HoverButton
# ---------------------------------------------------------------------------

class HoverButton(tk.Frame):
    """Frame-based button with smooth background transition on hover.

    Parameters
    ----------
    parent : tk.Widget
    text : str
        Button label.
    command : callable, optional
        Invoked on click.
    icon : str, optional
        Unicode character prepended to the label.
    fg : str
        Text colour.
    bg : str
        Default background.
    hover_bg : str
        Background on hover.
    active_bg : str, optional
        Background on click (defaults to *hover_bg*).
    font : tuple, optional
        Font spec; defaults to (Segoe UI, 11).
    padx : int
        Horizontal padding inside the button.
    pady : int
        Vertical padding inside the button.
    width : int, optional
        Label width in characters.
    """

    _ANIM_STEPS: int = 3
    _ANIM_INTERVAL_MS: int = 25

    def __init__(
        self,
        parent: tk.Widget,
        text: str = "",
        command: Optional[Callable] = None,
        icon: str = "",
        fg: str = Color.fg_bright,
        bg: str = Color.btn_neutral,
        hover_bg: str = Color.btn_neutral_hover,
        active_bg: Optional[str] = None,
        font: Optional[tuple] = None,
        padx: int = 14,
        pady: int = 6,
        width: Optional[int] = None,
        **kwargs,
    ) -> None:
        super().__init__(parent, bg=bg, cursor="hand2", **kwargs)

        self._command = command
        self._bg = bg
        self._hover_bg = hover_bg
        self._active_bg = active_bg or hover_bg
        self._current_bg = bg
        self._anim_id: Optional[str] = None
        self._enabled = True

        display_text = f"{icon}  {text}" if icon else text
        label_font = font or (Font.family, Font.size_btn)

        self._label = tk.Label(
            self,
            text=display_text,
            font=label_font,
            fg=fg,
            bg=bg,
            cursor="hand2",
        )
        if width is not None:
            self._label.configure(width=width)
        self._label.pack(padx=padx, pady=pady)

        # Bindings on both frame and label
        for w in (self, self._label):
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
            w.bind("<Button-1>", self._on_press)
            w.bind("<ButtonRelease-1>", self._on_release)

    # -- State ----------------------------------------------------------------

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if enabled:
            self._label.configure(fg=Color.fg_bright)
            self.configure(cursor="hand2")
            self._label.configure(cursor="hand2")
        else:
            self._label.configure(fg=Color.disabled)
            self.configure(cursor="arrow")
            self._label.configure(cursor="arrow")

    def set_text(self, text: str, icon: str = "") -> None:
        display = f"{icon}  {text}" if icon else text
        self._label.configure(text=display)

    # -- Animation ------------------------------------------------------------

    def _animate_to(self, target: str) -> None:
        if self._anim_id:
            self.after_cancel(self._anim_id)
            self._anim_id = None
        self._anim_step(self._current_bg, target, 0)

    def _anim_step(self, start: str, end: str, step: int) -> None:
        if step > self._ANIM_STEPS:
            return
        t = step / self._ANIM_STEPS
        colour = _lerp_color(start, end, t)
        self._current_bg = colour
        self.configure(bg=colour)
        self._label.configure(bg=colour)
        if step < self._ANIM_STEPS:
            self._anim_id = self.after(
                self._ANIM_INTERVAL_MS,
                self._anim_step, start, end, step + 1,
            )

    # -- Events ---------------------------------------------------------------

    def _on_enter(self, _event: tk.Event) -> None:
        if self._enabled:
            self._animate_to(self._hover_bg)

    def _on_leave(self, _event: tk.Event) -> None:
        if self._enabled:
            self._animate_to(self._bg)

    def _on_press(self, _event: tk.Event) -> None:
        if self._enabled:
            self.configure(bg=self._active_bg)
            self._label.configure(bg=self._active_bg)
            self._current_bg = self._active_bg

    def _on_release(self, _event: tk.Event) -> None:
        if self._enabled and self._command:
            self._command()


# ---------------------------------------------------------------------------
# GlowFrame
# ---------------------------------------------------------------------------

class GlowFrame(tk.Canvas):
    """Canvas wrapper that simulates a glow border around inner content.

    The inner content frame is accessible as ``.interior``.

    Parameters
    ----------
    parent : tk.Widget
    glow_color : str
        Base colour for the glow effect.
    glow_width : int
        Width of the glow in pixels (each side).
    content_bg : str
        Background for the interior frame.
    """

    def __init__(
        self,
        parent: tk.Widget,
        glow_color: str = Color.glow_primary,
        glow_width: int = 3,
        content_bg: str = Color.bg_card,
        **kwargs,
    ) -> None:
        bg = kwargs.pop("bg", Color.bg_primary)
        super().__init__(parent, bg=bg, highlightthickness=0, **kwargs)

        self._glow_color = glow_color
        self._glow_width = glow_width
        self._content_bg = content_bg

        self.interior = tk.Frame(self, bg=content_bg)
        self._win_id = self.create_window(0, 0, anchor="nw", window=self.interior)

        self.bind("<Configure>", self._redraw)

    def _redraw(self, _event: tk.Event | None = None) -> None:
        self.delete("glow")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 2 or h < 2:
            return

        gw = self._glow_width
        base_r, base_g, base_b = _hex_to_rgb(self._glow_color)
        bg_r, bg_g, bg_b = _hex_to_rgb(str(self.cget("bg")))

        # Draw glow layers from outermost (faintest) to innermost (brightest)
        for i in range(gw, 0, -1):
            t = 1.0 - (i / gw)  # 0 = outermost, ~1 = innermost
            opacity = 0.15 + 0.35 * t
            r = int(bg_r + (base_r - bg_r) * opacity)
            g = int(bg_g + (base_g - bg_g) * opacity)
            b = int(bg_b + (base_b - bg_b) * opacity)
            colour = _rgb_to_hex(r, g, b)
            self.create_rectangle(
                gw - i, gw - i, w - (gw - i), h - (gw - i),
                outline=colour, width=1, tags="glow",
            )

        # Position interior inside the glow
        self.itemconfigure(self._win_id, width=w - 2 * gw, height=h - 2 * gw)
        self.coords(self._win_id, gw, gw)

    def set_glow_color(self, color: str) -> None:
        self._glow_color = color
        self._redraw()


# ---------------------------------------------------------------------------
# GradientBar
# ---------------------------------------------------------------------------

class GradientBar(tk.Canvas):
    """Canvas that draws a horizontal gradient and hosts content above it.

    Access ``.interior`` to pack child widgets on top of the gradient.
    """

    def __init__(
        self,
        parent: tk.Widget,
        color_start: str = Color.bg_elevated,
        color_end: str = Color.bg_primary,
        height: int = Dim.toolbar_height,
        **kwargs,
    ) -> None:
        super().__init__(
            parent, height=height, highlightthickness=0,
            bg=color_start, **kwargs,
        )
        self._color_start = color_start
        self._color_end = color_end

        self.interior = tk.Frame(self, bg=color_start)
        self._win_id = self.create_window(0, 0, anchor="nw", window=self.interior)

        self.bind("<Configure>", self._redraw)

    def _redraw(self, _event: tk.Event | None = None) -> None:
        self.delete("gradient")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 2 or h < 2:
            return

        # Draw vertical gradient (top to bottom)
        r1, g1, b1 = _hex_to_rgb(self._color_start)
        r2, g2, b2 = _hex_to_rgb(self._color_end)
        # Draw in bands of 4 pixels for performance
        band = 4
        for y in range(0, h, band):
            t = y / max(h - 1, 1)
            colour = _rgb_to_hex(
                int(r1 + (r2 - r1) * t),
                int(g1 + (g2 - g1) * t),
                int(b1 + (b2 - b1) * t),
            )
            self.create_rectangle(
                0, y, w, y + band, fill=colour, outline=colour, tags="gradient",
            )

        # Ensure interior is on top
        self.tag_raise(self._win_id)
        self.itemconfigure(self._win_id, width=w, height=h)
        self.coords(self._win_id, 0, 0)


# ---------------------------------------------------------------------------
# PulseIndicator
# ---------------------------------------------------------------------------

class PulseIndicator(tk.Canvas):
    """Animated pulsing glow circle for recording indicators.

    Parameters
    ----------
    parent : tk.Widget
    color : str
        Circle fill colour.
    size : int
        Canvas width/height in pixels.
    pulse : bool
        Whether to start pulsing immediately.
    """

    _PULSE_INTERVAL_MS: int = 50
    _PULSE_STEPS: int = 20  # steps per half-cycle

    def __init__(
        self,
        parent: tk.Widget,
        color: str = Color.recording_dot,
        size: int = 14,
        pulse: bool = False,
        bg: str = "",
        **kwargs,
    ) -> None:
        if not bg:
            bg = str(parent.cget("bg"))
        super().__init__(
            parent, width=size, height=size,
            bg=bg, highlightthickness=0, **kwargs,
        )
        self._color = color
        self._bg = bg
        self._size = size
        self._pulsing = False
        self._step = 0
        self._direction = 1  # 1 = brightening, -1 = dimming
        self._after_id: Optional[str] = None

        # Draw core circle
        margin = max(2, size // 5)
        self._core = self.create_oval(
            margin, margin, size - margin, size - margin,
            fill=color, outline="",
        )
        # Outer glow ring
        self._glow = self.create_oval(
            1, 1, size - 1, size - 1,
            fill="", outline=color, width=1,
        )

        if pulse:
            self.start_pulse()

    def start_pulse(self) -> None:
        if self._pulsing:
            return
        self._pulsing = True
        self._step = 0
        self._direction = 1
        self._pulse_tick()

    def stop_pulse(self) -> None:
        self._pulsing = False
        if self._after_id:
            self.after_cancel(self._after_id)
            self._after_id = None
        # Reset to full colour
        self.itemconfigure(self._core, fill=self._color)
        self.itemconfigure(self._glow, outline=self._color)

    def _pulse_tick(self) -> None:
        if not self._pulsing:
            return

        t = self._step / self._PULSE_STEPS
        # Core: interpolate between color and a dim version
        dim = _lerp_color(self._color, self._bg, 0.6)
        core_color = _lerp_color(self._color, dim, t)
        self.itemconfigure(self._core, fill=core_color)

        # Glow ring: fade in/out
        glow_opacity = 1.0 - t * 0.8
        glow_color = _lerp_color(self._bg, self._color, glow_opacity)
        self.itemconfigure(self._glow, outline=glow_color)

        self._step += self._direction
        if self._step >= self._PULSE_STEPS:
            self._direction = -1
        elif self._step <= 0:
            self._direction = 1

        self._after_id = self.after(self._PULSE_INTERVAL_MS, self._pulse_tick)


# ---------------------------------------------------------------------------
# SessionCard
# ---------------------------------------------------------------------------

class SessionCard(tk.Frame):
    """Rich card widget for the session list sidebar.

    Displays date, duration, version count, and diarisation status
    with hover and selection effects.

    Parameters
    ----------
    parent : tk.Widget
    date_text : str
        Primary display text (e.g. "Mar 16 -- 1:58 PM").
    meta_text : str
        Secondary line (e.g. "5 min  |  3 versions").
    is_diarized : bool
        Whether to show the diarisation accent on the left bar.
    on_click : callable, optional
        Invoked when the card is clicked.
    """

    def __init__(
        self,
        parent: tk.Widget,
        date_text: str = "",
        meta_text: str = "",
        is_diarized: bool = False,
        on_click: Optional[Callable] = None,
        **kwargs,
    ) -> None:
        super().__init__(
            parent,
            bg=Color.bg_card,
            cursor="hand2",
            **kwargs,
        )
        self._on_click = on_click
        self._selected = False
        self._bg = Color.bg_card
        self._hover_bg = Color.bg_card_hover
        self._select_bg = Color.bg_card_selected
        self._current_bg = self._bg

        # Left accent bar
        accent_color = Color.glow_primary if is_diarized else Color.border_subtle
        self._accent_bar = tk.Frame(self, bg=accent_color, width=3)
        self._accent_bar.pack(side=tk.LEFT, fill=tk.Y)
        self._accent_default = accent_color

        # Content area
        content = tk.Frame(self, bg=Color.bg_card)
        content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                     padx=(Dim.card_padding_x, Dim.card_padding_x),
                     pady=Dim.card_padding_y)
        self._content = content

        # Date line
        self._date_label = tk.Label(
            content, text=date_text,
            font=(Font.family, Font.size_body),
            fg=Color.fg_primary, bg=Color.bg_card,
            anchor="w",
        )
        self._date_label.pack(fill=tk.X)

        # Meta line
        self._meta_label = tk.Label(
            content, text=meta_text,
            font=(Font.family, Font.size_small),
            fg=Color.fg_secondary, bg=Color.bg_card,
            anchor="w",
        )
        self._meta_label.pack(fill=tk.X, pady=(2, 0))

        # Bind hover + click on all children
        for w in (self, self._accent_bar, content,
                  self._date_label, self._meta_label):
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
            w.bind("<Button-1>", self._on_click_event)

    # -- Selection ------------------------------------------------------------

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        bg = self._select_bg if selected else self._bg
        self._set_bg(bg)
        if selected:
            self._accent_bar.configure(bg=Color.glow_primary)
        else:
            self._accent_bar.configure(bg=self._accent_default)

    # -- Internal -------------------------------------------------------------

    def _set_bg(self, bg: str) -> None:
        self._current_bg = bg
        self.configure(bg=bg)
        self._content.configure(bg=bg)
        self._date_label.configure(bg=bg)
        self._meta_label.configure(bg=bg)

    def _on_enter(self, _event: tk.Event) -> None:
        if not self._selected:
            self._set_bg(self._hover_bg)

    def _on_leave(self, _event: tk.Event) -> None:
        if not self._selected:
            self._set_bg(self._bg)

    def _on_click_event(self, _event: tk.Event) -> None:
        if self._on_click:
            self._on_click()

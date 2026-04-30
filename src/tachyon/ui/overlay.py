"""Transparent caption overlay for live transcription display.

Displays the last few transcript lines as closed-caption-style subtitles in
a semi-transparent, always-on-top, borderless tkinter window.  Positioned at
the bottom-center of the primary monitor by default.

Features
--------
- **Title bar** with drag handle, expand/collapse toggle, and close button
- **Collapsed mode** (default): last 4 caption lines, compact view
- **Expanded mode**: full scrollable transcript history with auto-scroll
- **Close button**: hides overlay, notifies caller via ``on_close`` callback
- **Recording indicator**: pulsing red dot when actively transcribing
- **Fade transitions**: smooth opacity on show/hide
- **Thread-safe**: all visibility methods schedule on the tkinter thread

Thread safety
-------------
Tkinter is **not** thread-safe.  All transcript data arrives via a
``queue.Queue`` of ``TranscriptSegment`` objects.  The overlay polls this
queue every 100 ms using ``root.after()`` so that widget updates always
happen on the tkinter (main) thread.
"""

from __future__ import annotations

import logging
import queue
import tkinter as tk
from typing import Callable, List, Optional

from tachyon.session import TranscriptSegment
from tachyon.ui.theme import Color, Font, Dim, OVERLAY_SPEAKER_PALETTE
from tachyon.ui.widgets import PulseIndicator

logger = logging.getLogger(__name__)


_PLACEHOLDER_TEXT: str = (
    "Tachyon — live captions will appear here while recording.\n"
    "Drag to reposition  ·  Right-click the tray icon to control."
)


class CaptionOverlay:
    """Always-on-top caption overlay window with expand/collapse and close.

    Parameters
    ----------
    segment_queue:
        A ``queue.Queue`` that receives ``TranscriptSegment`` objects from the
        transcriber thread.
    position:
        ``(x, y)`` screen coordinates for the top-left corner of the overlay.
        When *None* (the default), the overlay is placed at the bottom-center
        of the primary monitor.
    opacity:
        Window opacity from ``0.0`` (invisible) to ``1.0`` (opaque).
        Defaults to ``0.8``.
    on_close:
        Optional callback invoked when the user clicks the close button.
        Called on the tkinter main thread.
    """

    def __init__(
        self,
        segment_queue: queue.Queue,  # queue.Queue[TranscriptSegment]
        position: Optional[tuple[int, int]] = None,
        opacity: float = 0.8,
        on_close: Optional[Callable[[], None]] = None,
    ) -> None:
        self._segment_queue: queue.Queue = segment_queue
        self._position: Optional[tuple[int, int]] = position
        self._opacity: float = max(0.0, min(1.0, opacity))
        self._on_close: Optional[Callable[[], None]] = on_close

        # Caption state
        self._lines: List[TranscriptSegment] = []  # last N for collapsed view
        self._all_segments: List[TranscriptSegment] = []  # full history
        self._visible: bool = True
        self._expanded: bool = False
        self._user_scrolled_up: bool = False

        # Dynamic speaker color tracking (for multi-loopback support)
        self._speaker_color_map: dict[str, str] = {}
        self._next_speaker_color_idx: int = 0

        # Recording indicator state
        self._recording: bool = False

        # Fade state
        self._fade_id: Optional[str] = None
        self._app_icon_photo: object | None = None

        # -- Drag state ------------------------------------------------------
        self._drag_offset_x: int = 0
        self._drag_offset_y: int = 0

        # -- Build the tkinter UI --------------------------------------------
        self._root: tk.Tk = tk.Tk()
        self._root.withdraw()  # hide until fully configured
        self._set_window_icon()
        self._configure_window()
        self._build_widgets()
        self._bind_drag_events()

        # Show placeholder text so the overlay has visible body before any
        # captions arrive — otherwise the window collapses to a hard-to-grab
        # ~1px stripe under the title bar.
        self._show_placeholder()

        # Position the window (needs to happen after widgets are laid out)
        self._root.update_idletasks()
        self._apply_position()
        self._recalc_collapsed_height()

        # Show the window now that everything is ready
        self._root.deiconify()

        # Kick off queue polling
        self._poll_id: Optional[str] = None
        self._schedule_poll()

    # ------------------------------------------------------------------
    # Window setup helpers
    # ------------------------------------------------------------------

    def _configure_window(self) -> None:
        """Apply window-level attributes: borderless, topmost, transparent."""
        self._root.title("Tachyon Captions")
        self._root.overrideredirect(True)
        self._root.attributes("-topmost", True)
        self._root.attributes("-alpha", self._opacity)
        self._root.configure(bg=Color.overlay_bg)

        # Prevent the window from appearing in the taskbar on Windows.
        try:
            self._root.attributes("-toolwindow", True)
        except tk.TclError:
            pass  # not available on all platforms

        # Fixed width; height adapts to content
        self._root.geometry(f"{Dim.overlay_width}x1")
        self._root.resizable(False, False)

    def _set_window_icon(self) -> None:
        """Use the Tachyon icon for the root window and child dialogs."""
        try:
            from PIL import ImageTk
            from tachyon.ui.tray import create_app_icon

            icon_img = create_app_icon(recording=False)
            self._app_icon_photo = ImageTk.PhotoImage(icon_img)
            self._root.iconphoto(True, self._app_icon_photo)
        except Exception:
            logger.debug("Failed to set Tk window icon", exc_info=True)

    def _build_widgets(self) -> None:
        """Build the full widget hierarchy."""
        # Main container frame with glow border
        self._main_frame = tk.Frame(
            self._root, bg=Color.overlay_bg,
            highlightbackground=Color.glow_secondary,
            highlightthickness=1,
        )
        self._main_frame.pack(fill=tk.BOTH, expand=True)

        # HUD edge line (bright cyan accent at top)
        tk.Frame(
            self._main_frame, bg=Color.glow_primary, height=2,
        ).pack(fill=tk.X, side=tk.TOP)

        # -- Title bar -------------------------------------------------------
        self._titlebar = tk.Frame(
            self._main_frame, bg=Color.overlay_titlebar,
            height=Dim.titlebar_height,
        )
        self._titlebar.pack(fill=tk.X, side=tk.TOP)
        self._titlebar.pack_propagate(False)

        # Recording indicator (animated pulsing glow)
        self._pulse_indicator = PulseIndicator(
            self._titlebar,
            color=Color.recording_dot,
            size=14,
            pulse=False,
            bg=Color.overlay_titlebar,
        )
        # Not packed yet — shown when recording starts

        # Drag label (left side)
        self._drag_label = tk.Label(
            self._titlebar,
            text="  TACHYON",
            font=(Font.family, Font.size_titlebar),
            fg=Color.fg_muted,
            bg=Color.overlay_titlebar,
            anchor=tk.W,
        )
        self._drag_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Close button (right side — pack first so it's rightmost)
        self._close_btn = tk.Label(
            self._titlebar,
            text=" \u2715 ",
            font=(Font.family, Font.size_titlebar),
            fg=Color.fg_muted,
            bg=Color.overlay_titlebar,
            cursor="hand2",
        )
        self._close_btn.pack(side=tk.RIGHT, padx=(0, 4))
        self._close_btn.bind("<Button-1>", self._on_close_click)
        self._close_btn.bind("<Enter>", self._on_close_enter)
        self._close_btn.bind("<Leave>", self._on_close_leave)

        # Expand/collapse button
        self._expand_btn = tk.Label(
            self._titlebar,
            text=" \u25b2 ",  # ▲ = collapsed (click to expand up)
            font=(Font.family, Font.size_titlebar),
            fg=Color.fg_muted,
            bg=Color.overlay_titlebar,
            cursor="hand2",
        )
        self._expand_btn.pack(side=tk.RIGHT, padx=(0, 2))
        self._expand_btn.bind("<Button-1>", self._on_expand_click)
        self._expand_btn.bind("<Enter>", self._on_expand_enter)
        self._expand_btn.bind("<Leave>", self._on_expand_leave)

        # -- Caption label (collapsed mode) ----------------------------------
        self._caption_label = tk.Label(
            self._main_frame,
            text="",
            font=(Font.family, 15),
            fg=Color.fg_bright,
            bg=Color.overlay_bg,
            wraplength=Dim.overlay_width - 2 * Dim.overlay_padding_x,
            justify=tk.LEFT,
            anchor=tk.W,
            padx=Dim.overlay_padding_x,
            pady=Dim.overlay_padding_y,
        )
        self._caption_label.pack(fill=tk.BOTH, expand=True)

        # -- Expanded frame (hidden by default) ------------------------------
        self._expanded_frame = tk.Frame(self._main_frame, bg=Color.overlay_bg)
        # Not packed yet — shown only in expanded mode

        self._text_widget = tk.Text(
            self._expanded_frame,
            font=(Font.family, Font.size_title),
            fg=Color.fg_bright,
            bg=Color.overlay_bg,
            wrap=tk.WORD,
            state=tk.DISABLED,
            borderwidth=0,
            highlightthickness=0,
            padx=Dim.overlay_padding_x,
            pady=Dim.overlay_padding_y,
            spacing3=4,
            cursor="arrow",
            selectbackground=Color.selection,
            selectforeground=Color.fg_bright,
        )

        self._scrollbar = tk.Scrollbar(
            self._expanded_frame,
            command=self._text_widget.yview,
            bg=Color.overlay_bg,
            troughcolor=Color.scrollbar_trough,
        )

        self._text_widget.configure(yscrollcommand=self._on_scroll_changed)

        self._scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._text_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Configure text tags (speaker tags created dynamically)
        self._text_widget.tag_configure("text_normal", foreground=Color.fg_bright)
        self._text_widget.tag_configure(
            "divider", foreground=Color.border_subtle,
        )

        # Bind mousewheel for scroll detection
        self._text_widget.bind("<MouseWheel>", self._on_mousewheel)

    def _apply_position(self) -> None:
        """Position the window on screen.

        If a position was supplied, use it directly.  Otherwise, place the
        overlay at the horizontal center near the bottom of the primary
        monitor.
        """
        if self._position is not None:
            x, y = self._position
        else:
            screen_w: int = self._root.winfo_screenwidth()
            screen_h: int = self._root.winfo_screenheight()
            win_h: int = self._root.winfo_reqheight()
            x = (screen_w - Dim.overlay_width) // 2
            y = screen_h - win_h - Dim.overlay_bottom_margin

        self._root.geometry(f"+{x}+{y}")

    # ------------------------------------------------------------------
    # Drag-to-reposition
    # ------------------------------------------------------------------

    def _bind_drag_events(self) -> None:
        """Allow click-and-drag repositioning via title bar and caption label."""
        for widget in (self._titlebar, self._drag_label, self._caption_label):
            widget.bind("<ButtonPress-1>", self._on_drag_start)
            widget.bind("<B1-Motion>", self._on_drag_motion)

    def _on_drag_start(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        """Record the cursor offset relative to the window origin."""
        self._drag_offset_x = event.x_root - self._root.winfo_x()
        self._drag_offset_y = event.y_root - self._root.winfo_y()

    def _on_drag_motion(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        """Move the window to follow the cursor."""
        x: int = event.x_root - self._drag_offset_x
        y: int = event.y_root - self._drag_offset_y
        self._root.geometry(f"+{x}+{y}")

    # ------------------------------------------------------------------
    # Title bar button handlers
    # ------------------------------------------------------------------

    def _on_close_click(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        """Hide the overlay and notify the caller."""
        self._hide_impl()
        if self._on_close is not None:
            self._on_close()

    def _on_close_enter(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        self._close_btn.configure(bg=Color.danger, fg=Color.fg_bright)

    def _on_close_leave(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        self._close_btn.configure(bg=Color.overlay_titlebar, fg=Color.fg_muted)

    def _on_expand_click(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if self._expanded:
            self._collapse()
        else:
            self._expand()

    def _on_expand_enter(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        self._expand_btn.configure(bg=Color.overlay_btn_hover)

    def _on_expand_leave(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        self._expand_btn.configure(bg=Color.overlay_titlebar)

    # ------------------------------------------------------------------
    # Expand / Collapse
    # ------------------------------------------------------------------

    def _expand(self) -> None:
        """Switch to expanded mode with scrollable transcript."""
        self._expanded = True
        self._expand_btn.configure(text=" \u25bc ")  # ▼

        # Hide caption label, show expanded frame
        self._caption_label.pack_forget()
        self._expanded_frame.pack(fill=tk.BOTH, expand=True)

        # Populate text widget with full history
        self._repopulate_text_widget()

        # Resize window
        cur_x: int = self._root.winfo_x()
        cur_y: int = self._root.winfo_y()

        # Check if expanded window would go off-screen bottom
        screen_h: int = self._root.winfo_screenheight()
        if cur_y + Dim.expanded_height > screen_h:
            cur_y = max(0, screen_h - Dim.expanded_height - 10)

        self._root.geometry(
            f"{Dim.expanded_width}x{Dim.expanded_height}+{cur_x}+{cur_y}"
        )

        # Reset scroll state — auto-scroll to bottom
        self._user_scrolled_up = False
        self._scroll_to_bottom()

    def _collapse(self) -> None:
        """Switch back to collapsed compact view."""
        self._expanded = False
        self._expand_btn.configure(text=" \u25b2 ")  # ▲

        # Hide expanded frame, show caption label
        self._expanded_frame.pack_forget()
        self._caption_label.pack(fill=tk.BOTH, expand=True)

        self._recalc_collapsed_height()

    def _repopulate_text_widget(self) -> None:
        """Fill the text widget with all segments from history."""
        self._text_widget.configure(state=tk.NORMAL)
        self._text_widget.delete("1.0", tk.END)
        prev_speaker: Optional[str] = None
        for seg in self._all_segments:
            # Insert divider between different speakers
            if prev_speaker is not None and seg.speaker != prev_speaker:
                self._text_widget.insert(
                    tk.END, "\u2500" * 40 + "\n", "divider",
                )
            self._append_segment_to_text(seg)
            prev_speaker = seg.speaker
        self._text_widget.configure(state=tk.DISABLED)

    def _get_speaker_tag(self, speaker: str) -> str:
        """Get or create a text tag for a speaker, assigning a color dynamically."""
        tag_name = f"speaker_{speaker.replace(' ', '_').lower()}"
        if speaker not in self._speaker_color_map:
            if speaker == "You":
                color = Color.speaker_you
            else:
                color = OVERLAY_SPEAKER_PALETTE[
                    self._next_speaker_color_idx % len(OVERLAY_SPEAKER_PALETTE)
                ]
                self._next_speaker_color_idx += 1
            self._speaker_color_map[speaker] = color
            self._text_widget.tag_configure(tag_name, foreground=color)
        return tag_name

    def _append_segment_to_text(self, seg: TranscriptSegment) -> None:
        """Append a single segment to the text widget with speaker coloring."""
        speaker_tag = self._get_speaker_tag(seg.speaker)

        # Add newline before all segments except the first
        if self._text_widget.index("end-1c") != "1.0":
            self._text_widget.insert(tk.END, "\n", "text_normal")

        self._text_widget.insert(tk.END, f"{seg.speaker}: ", speaker_tag)
        self._text_widget.insert(tk.END, seg.text, "text_normal")

    # ------------------------------------------------------------------
    # Recording indicator
    # ------------------------------------------------------------------

    def set_recording(self, active: bool) -> None:
        """Show or hide the pulsing recording indicator (thread-safe)."""
        self._root.after(0, lambda: self._set_recording_impl(active))

    def _set_recording_impl(self, active: bool) -> None:
        self._recording = active
        if active:
            self._start_recording_pulse()
        else:
            self._stop_recording_pulse()

    def _start_recording_pulse(self) -> None:
        """Start the pulsing glow animation."""
        self._pulse_indicator.pack(side=tk.LEFT, padx=(6, 0))
        self._pulse_indicator.start_pulse()

    def _stop_recording_pulse(self) -> None:
        """Stop the pulsing animation and hide the indicator."""
        self._pulse_indicator.stop_pulse()
        self._pulse_indicator.pack_forget()

    # ------------------------------------------------------------------
    # Scroll detection
    # ------------------------------------------------------------------

    def _on_scroll_changed(self, first: str, last: str) -> None:
        """yscrollcommand callback — detect if user has scrolled up."""
        self._scrollbar.set(first, last)
        if float(last) < 0.99:
            self._user_scrolled_up = True
        else:
            self._user_scrolled_up = False

    def _on_mousewheel(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        """Re-check scroll position after mousewheel with a small delay."""
        self._root.after(50, self._check_scroll_position)

    def _check_scroll_position(self) -> None:
        """Check if we're at the bottom and update auto-scroll flag."""
        try:
            last = float(self._scrollbar.get()[1])
            self._user_scrolled_up = last < 0.99
        except (ValueError, IndexError):
            pass

    def _scroll_to_bottom(self) -> None:
        """Scroll the text widget to the bottom."""
        self._text_widget.see(tk.END)

    # ------------------------------------------------------------------
    # Queue polling (thread-safe segment consumption)
    # ------------------------------------------------------------------

    def _schedule_poll(self) -> None:
        """Register the next poll callback."""
        self._poll_id = self._root.after(Dim.overlay_poll_ms, self._poll_queue)

    def _poll_queue(self) -> None:
        """Drain all pending segments from the queue, update the display.

        Called on the tkinter main thread via ``root.after()``.  Reschedules
        itself to keep polling indefinitely.
        """
        new_segments: List[TranscriptSegment] = []
        try:
            while True:
                segment: TranscriptSegment = self._segment_queue.get_nowait()
                self._all_segments.append(segment)
                self._lines.append(segment)
                new_segments.append(segment)
        except queue.Empty:
            pass

        # Keep only the most recent lines for collapsed view
        if len(self._lines) > Dim.overlay_max_lines:
            self._lines = self._lines[-Dim.overlay_max_lines:]

        if new_segments:
            self._update_collapsed_display()
            if self._expanded:
                self._append_new_segments_to_text(new_segments)

        # Reschedule
        self._schedule_poll()

    # ------------------------------------------------------------------
    # Display rendering
    # ------------------------------------------------------------------

    def _update_collapsed_display(self) -> None:
        """Refresh the caption label with the current lines (collapsed view)."""
        if not self._lines:
            self._show_placeholder()
        else:
            formatted: List[str] = [
                f"{seg.speaker}: {seg.text}" for seg in self._lines
            ]
            self._caption_label.configure(
                text="\n".join(formatted),
                fg=Color.fg_bright,
            )

        if not self._expanded:
            self._recalc_collapsed_height()

    def _show_placeholder(self) -> None:
        """Fill the caption label with muted placeholder copy.

        Used at startup and whenever ``clear_history()`` empties the
        line buffer — keeps the overlay visually present and draggable
        even when there's no live transcription to show.  Does not
        touch geometry; pair with :meth:`_recalc_collapsed_height` if
        the window size needs to be updated.
        """
        self._caption_label.configure(
            text=_PLACEHOLDER_TEXT,
            fg=Color.fg_muted,
        )

    def _recalc_collapsed_height(self) -> None:
        """Resize the (collapsed) window to fit the caption label content."""
        if self._expanded:
            return
        self._root.update_idletasks()
        label_h: int = self._caption_label.winfo_reqheight()
        total_h: int = Dim.titlebar_height + max(label_h, 1)
        cur_x: int = self._root.winfo_x()
        cur_y: int = self._root.winfo_y()
        self._root.geometry(
            f"{Dim.overlay_width}x{total_h}+{cur_x}+{cur_y}"
        )

    def _append_new_segments_to_text(
        self, segments: List[TranscriptSegment],
    ) -> None:
        """Incrementally append new segments to the expanded text widget."""
        self._text_widget.configure(state=tk.NORMAL)
        for seg in segments:
            self._append_segment_to_text(seg)
        self._text_widget.configure(state=tk.DISABLED)

        # Auto-scroll to bottom unless user has scrolled up
        if not self._user_scrolled_up:
            self._scroll_to_bottom()

    # ------------------------------------------------------------------
    # Visibility controls (thread-safe via root.after)
    # ------------------------------------------------------------------

    def show(self) -> None:
        """Make the overlay visible (thread-safe)."""
        self._root.after(0, self._show_impl)

    def hide(self) -> None:
        """Hide the overlay (thread-safe, keeps polling)."""
        self._root.after(0, self._hide_impl)

    def toggle(self) -> None:
        """Toggle overlay visibility (thread-safe)."""
        self._root.after(0, self._toggle_impl)

    def _show_impl(self) -> None:
        self._visible = True
        # Fade in
        self._fade_to(self._opacity)
        self._root.deiconify()
        self._root.attributes("-topmost", True)

    def _hide_impl(self) -> None:
        self._visible = False
        self._root.withdraw()

    def _toggle_impl(self) -> None:
        if self._visible:
            self._hide_impl()
        else:
            self._show_impl()

    # ------------------------------------------------------------------
    # Fade transitions
    # ------------------------------------------------------------------

    def _fade_to(self, target: float, step: float = 0.1) -> None:
        """Smoothly transition alpha to target over ~200ms."""
        if self._fade_id:
            self._root.after_cancel(self._fade_id)
            self._fade_id = None

        try:
            current = float(self._root.attributes("-alpha"))
        except (tk.TclError, ValueError):
            current = self._opacity

        if abs(current - target) < step:
            self._root.attributes("-alpha", target)
            return

        direction = step if target > current else -step
        new_alpha = max(0.0, min(1.0, current + direction))
        self._root.attributes("-alpha", new_alpha)
        self._fade_id = self._root.after(
            20, lambda: self._fade_to(target, step)
        )

    # ------------------------------------------------------------------
    # History management
    # ------------------------------------------------------------------

    def clear_history(self) -> None:
        """Clear all transcript history (thread-safe).

        Resets both the collapsed caption lines and the full segment history.
        Call this when starting a new recording session.
        """
        self._root.after(0, self._clear_history_impl)

    def _clear_history_impl(self) -> None:
        self._all_segments.clear()
        self._lines.clear()
        self._speaker_color_map.clear()
        self._next_speaker_color_idx = 0
        self._show_placeholder()
        if not self._expanded:
            self._recalc_collapsed_height()

        # Clear text widget
        self._text_widget.configure(state=tk.NORMAL)
        self._text_widget.delete("1.0", tk.END)
        self._text_widget.configure(state=tk.DISABLED)

        self._user_scrolled_up = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the tkinter main loop (blocking).

        This should be called from the main thread.  It blocks until
        :meth:`destroy` is called.
        """
        logger.info("Caption overlay entering mainloop")
        self._root.mainloop()

    def destroy(self) -> None:
        """Cleanly shut down the overlay and exit the main loop.

        Safe to call from any thread -- the actual teardown is scheduled on
        the tkinter thread via ``root.after()``.
        """
        logger.info("Caption overlay shutting down")
        try:
            if self._poll_id is not None:
                self._root.after_cancel(self._poll_id)
                self._poll_id = None
            self._pulse_indicator.stop_pulse()
            if self._fade_id is not None:
                self._root.after_cancel(self._fade_id)
                self._fade_id = None
        except tk.TclError:
            pass
        try:
            self._root.quit()
            self._root.destroy()
        except tk.TclError:
            # Window already destroyed -- nothing to do.
            pass

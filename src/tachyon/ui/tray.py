"""System tray icon and menu for Tachyon Transcripts.

Provides a pystray-based system tray icon with menu items for controlling
the application: start/stop recording, show/hide captions, set output
folder, and quit.  The tray icon is generated programmatically via Pillow
(no external asset needed) and changes appearance when recording is active.

Threading
---------
The pystray event loop is blocking, so ``TrayIcon.run()`` should be called
from a dedicated thread.  All menu-item callbacks are forwarded to the
caller-supplied functions, which must be thread-safe.

Any callback that needs to display a tkinter widget (such as the output
folder picker or the setup wizard) is invoked without arguments — the
caller is responsible for scheduling the dialog onto the tkinter main
thread via ``root.after(0, ...)``.  Tkinter widgets must **not** be
created from the pystray thread.

Usage::

    tray = TrayIcon(
        on_start=start_recording,
        on_stop=stop_recording,
        on_toggle_overlay=toggle_overlay,
        on_set_output_folder=set_output_folder,
        on_quit=quit_app,
    )
    threading.Thread(target=tray.run, daemon=True).start()
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageFont
import pystray

from tachyon.capture import AudioCapture
from tachyon.ui.theme import Dim

logger = logging.getLogger(__name__)


def create_app_icon(recording: bool = False) -> "Image.Image":
    """Generate the Tachyon Transcripts app icon.

    Module-level function so other components (e.g. reviewer window icon)
    can reuse it without instantiating TrayIcon.
    """
    # Render at 2x for anti-aliasing
    render_size = Dim.icon_size * 2
    image = Image.new("RGBA", (render_size, render_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    padding = 4
    cx, cy = render_size // 2, render_size // 2
    r = render_size // 2 - padding

    if recording:
        draw.ellipse(
            [padding, padding, render_size - padding, render_size - padding],
            fill=(92, 16, 16, 240),
        )
        inner_r = r - 16
        draw.ellipse(
            [cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r],
            fill=(140, 25, 25, 220),
        )
        draw.ellipse(
            [padding, padding, render_size - padding, render_size - padding],
            outline=(220, 50, 50, 160), width=3,
        )
    else:
        draw.ellipse(
            [padding, padding, render_size - padding, render_size - padding],
            fill=(10, 14, 23, 240),
        )
        inner_r = r - 16
        draw.ellipse(
            [cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r],
            fill=(15, 21, 32, 220),
        )
        draw.ellipse(
            [padding, padding, render_size - padding, render_size - padding],
            outline=(0, 180, 216, 100), width=3,
        )
        draw.ellipse(
            [padding + 1, padding + 1, render_size - padding - 1, render_size - padding - 1],
            outline=(30, 48, 80, 120), width=1,
        )

    try:
        font = ImageFont.truetype("segoeui.ttf", 72)
    except OSError:
        try:
            font = ImageFont.truetype("arial.ttf", 72)
        except OSError:
            font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), "T", font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    text_x = (render_size - text_w) / 2 - bbox[0]
    text_y = (render_size - text_h) / 2 - bbox[1]
    draw.text(
        (text_x, text_y), "T",
        fill=(255, 255, 255, 255),
        font=font,
    )

    bolt_color = (255, 150, 50, 230) if recording else (0, 180, 216, 220)
    bolt_x = int(text_x + text_w)
    bolt_y = int(text_y + text_h * 0.35)
    draw.line(
        [(bolt_x, bolt_y), (bolt_x - 6, bolt_y + 12),
         (bolt_x + 2, bolt_y + 10), (bolt_x - 4, bolt_y + 22)],
        fill=bolt_color, width=3,
    )

    return image.resize((Dim.icon_size, Dim.icon_size), Image.LANCZOS)


class TrayIcon:
    """System tray icon with contextual menu for Tachyon Transcripts.

    Parameters
    ----------
    on_start:
        Called when the user clicks "Start Recording".
    on_stop:
        Called when the user clicks "Stop Recording".
    on_toggle_overlay:
        Called when the user clicks "Show Captions" / "Hide Captions".
    on_set_output_folder:
        Called (with no arguments) when the user clicks "Set Output
        Folder...".  The caller must open the folder-picker dialog on
        the tkinter main thread — tkinter widgets cannot be created from
        the pystray thread.
    on_quit:
        Called when the user clicks "Quit".
    on_show_wizard:
        Optional callback invoked (no args) when the user clicks
        "Setup Wizard".  If ``None``, the menu item is hidden.  Caller
        must marshal onto the tkinter thread.
    """

    def __init__(
        self,
        on_start: Callable[[], None],
        on_stop: Callable[[], None],
        on_toggle_overlay: Callable[[], None],
        on_set_output_folder: Callable[[], None],
        on_open_output_folder: Callable[[], None],
        on_set_mic_device: Callable[[Optional[str]], None],
        on_set_loopback_devices: Callable[[list], None],
        on_review: Callable[[], None],
        on_quit: Callable[[], None],
        on_show_wizard: Optional[Callable[[], None]] = None,
    ) -> None:
        self._on_start = on_start
        self._on_stop = on_stop
        self._on_toggle_overlay = on_toggle_overlay
        self._on_set_output_folder = on_set_output_folder
        self._on_open_output_folder = on_open_output_folder
        self._on_set_mic_device = on_set_mic_device
        self._on_set_loopback_devices = on_set_loopback_devices
        self._on_review = on_review
        self._on_quit = on_quit
        self._on_show_wizard = on_show_wizard

        self._recording: bool = False
        self._captions_visible: bool = True
        self._current_mic: Optional[str] = None
        self._current_loopbacks: list[str] = []  # list of enabled device names
        self._batch_running: bool = False
        self._last_session_time: Optional[datetime] = None
        self._status_text: Optional[str] = None
        self._model_ready: bool = False

        self._icon: Optional[pystray.Icon] = None

        logger.debug("TrayIcon initialised")

    # ------------------------------------------------------------------
    # Icon image generation
    # ------------------------------------------------------------------

    def _create_icon(self, recording: bool = False) -> Image.Image:
        """Delegate to module-level :func:`create_app_icon`."""
        return create_app_icon(recording=recording)

    # ------------------------------------------------------------------
    # Menu construction
    # ------------------------------------------------------------------

    def _build_menu(self) -> pystray.Menu:
        """Build the context menu reflecting current state.

        Returns
        -------
        pystray.Menu
            A pystray ``Menu`` instance with all action items.
        """
        # Last session info item (disabled, informational)
        if self._last_session_time is not None:
            last_label = f"Last: {self._last_session_time.strftime('%b %d, %I:%M %p')}"
        else:
            last_label = "No sessions recorded"
        last_item = pystray.MenuItem(last_label, None, enabled=False)

        if self._recording:
            record_item = pystray.MenuItem("Stop Recording", self._handle_stop)
        else:
            record_item = pystray.MenuItem(
                "Start Recording",
                self._handle_start,
                enabled=self._model_ready and not self._batch_running,
            )

        if self._captions_visible:
            captions_item = pystray.MenuItem("Hide Captions", self._handle_toggle_overlay)
        else:
            captions_item = pystray.MenuItem("Show Captions", self._handle_toggle_overlay)

        # Build microphone selection submenu
        mic_submenu_items = self._build_mic_submenu()

        # Build loopback device submenu
        loopback_submenu_items = self._build_loopback_submenu()

        # Review transcripts — disabled during recording
        review_enabled = not self._recording
        review_item = pystray.MenuItem(
            "Review Transcripts",
            self._handle_review,
            enabled=review_enabled,
        )

        menu_items: list = []
        if self._status_text:
            menu_items.append(pystray.MenuItem(
                self._status_text, None, enabled=False,
            ))
            menu_items.append(pystray.Menu.SEPARATOR)

        menu_items += [
            last_item,
            pystray.Menu.SEPARATOR,
            record_item,
            captions_item,
            review_item,
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Set Microphone", pystray.Menu(*mic_submenu_items)),
            pystray.MenuItem("Loopback Devices", pystray.Menu(*loopback_submenu_items)),
            pystray.MenuItem("Set Output Folder...", self._handle_set_output_folder),
            pystray.MenuItem("Open Output Folder", self._handle_open_output_folder),
        ]

        # Setup Wizard — only shown if a handler was provided
        if self._on_show_wizard is not None:
            menu_items.append(pystray.Menu.SEPARATOR)
            menu_items.append(
                pystray.MenuItem("Setup Wizard", self._handle_show_wizard),
            )

        menu_items.append(pystray.Menu.SEPARATOR)
        menu_items.append(pystray.MenuItem("Quit", self._handle_quit))

        return pystray.Menu(*menu_items)

    # ------------------------------------------------------------------
    # Menu action handlers
    # ------------------------------------------------------------------

    def _handle_start(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        logger.debug("Menu: Start Recording")
        self._on_start()

    def _handle_stop(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        logger.debug("Menu: Stop Recording")
        self._on_stop()

    def _handle_toggle_overlay(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        logger.debug("Menu: Toggle Captions")
        self._on_toggle_overlay()

    def _handle_set_output_folder(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        """Forward the menu click to the app so it can display the dialog.

        The actual ``tkinter.filedialog`` must be opened on the tkinter
        main thread — creating a second ``Tk()`` on the pystray thread
        leaks resources and conflicts with the running root.  The caller
        is expected to schedule the dialog via ``root.after(0, ...)``.
        """
        logger.debug("Menu: Set Output Folder")
        self._on_set_output_folder()

    def _handle_open_output_folder(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        logger.debug("Menu: Open Output Folder")
        self._on_open_output_folder()

    def _handle_show_wizard(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        """Tray action: re-open the first-run setup wizard."""
        logger.debug("Menu: Setup Wizard")
        if self._on_show_wizard is not None:
            self._on_show_wizard()

    def _handle_set_mic_device(self, device_name: Optional[str]) -> None:
        """Set the active microphone device and notify the app."""
        self._current_mic = device_name
        self._on_set_mic_device(device_name)
        self._refresh()

    def _build_mic_submenu(self) -> list[pystray.MenuItem]:
        """Enumerate WASAPI input devices and build submenu items.

        Returns a list of ``pystray.MenuItem`` with a checkmark prefix on
        the currently selected device.
        """

        def _make_handler(device_name: Optional[str]):
            """Create a 2-arg callback capturing *device_name* via closure.

            pystray validates that action callables accept exactly (icon, item),
            so we cannot use a ``default=`` trick on the lambda.
            """
            return lambda icon, item: self._handle_set_mic_device(device_name)

        items: list[pystray.MenuItem] = []

        # "System Default" entry (maps to None)
        default_label = ("* System Default" if self._current_mic is None
                         else "  System Default")
        items.append(pystray.MenuItem(default_label, _make_handler(None)))

        # Enumerate WASAPI input devices
        try:
            devices = AudioCapture.get_devices()
        except Exception:
            logger.warning("Failed to enumerate audio devices for mic menu",
                           exc_info=True)
            return items

        for dev in devices:
            if dev.get("max_input_channels", 0) <= 0:
                continue
            name: str = dev["name"]
            label = f"* {name}" if name == self._current_mic else f"  {name}"
            items.append(pystray.MenuItem(label, _make_handler(name)))

        return items

    def _build_loopback_submenu(self) -> list[pystray.MenuItem]:
        """Enumerate WASAPI output devices and build loopback toggle submenu.

        Each output device is shown with a ``*`` prefix if currently enabled.
        Toggling a device adds/removes it from the enabled list and notifies
        the app.  Also includes a "System Default" entry.
        """

        def _make_toggle_handler(device_name: Optional[str]):
            return lambda icon, item: self._handle_toggle_loopback(device_name)

        items: list[pystray.MenuItem] = []

        # "System Default" entry
        has_explicit = len(self._current_loopbacks) > 0
        default_prefix = "* " if not has_explicit else "  "
        items.append(pystray.MenuItem(
            f"{default_prefix}System Default (single)",
            _make_toggle_handler(None),
        ))

        # Enumerate WASAPI output devices via loopback enumeration
        try:
            loopback_devices = AudioCapture.get_loopback_devices()
        except Exception:
            logger.warning("Failed to enumerate loopback devices", exc_info=True)
            return items

        for dev in loopback_devices:
            name: str = dev.get("name", "")
            if not name:
                continue
            is_enabled = name in self._current_loopbacks
            prefix = "* " if is_enabled else "  "
            items.append(pystray.MenuItem(
                f"{prefix}{name}",
                _make_toggle_handler(name),
            ))

        return items

    def _handle_toggle_loopback(self, device_name: Optional[str]) -> None:
        """Toggle a loopback device on or off.

        If ``device_name`` is None, reset to system default (single device).
        Otherwise, add/remove the device from the enabled list.
        """
        if device_name is None:
            # Reset to system default
            self._current_loopbacks = []
        else:
            if device_name in self._current_loopbacks:
                self._current_loopbacks.remove(device_name)
            else:
                self._current_loopbacks.append(device_name)

        self._on_set_loopback_devices(list(self._current_loopbacks))
        self._refresh()

    def _handle_review(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        logger.debug("Menu: Review Transcripts")
        self._on_review()

    def _handle_quit(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        logger.debug("Menu: Quit")
        self._on_quit()

    # ------------------------------------------------------------------
    # State updates
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        """Refresh the icon image and rebuild the menu.

        Called internally whenever observable state changes.
        """
        if self._icon is None:
            return
        try:
            self._icon.icon = self._create_icon(recording=self._recording)
            self._icon.menu = self._build_menu()
            self._icon.update_menu()
        except Exception:
            # State updates may arrive from non-tray threads (e.g. model-load
            # worker). Keep running even if pystray rejects a particular update.
            logger.warning("Tray refresh failed", exc_info=True)

    def set_recording(self, active: bool) -> None:
        """Update the tray to reflect whether a recording session is active.

        Changes the icon (swaps to red-tinted version) and swaps the menu
        label between "Start Recording" and "Stop Recording".

        Parameters
        ----------
        active:
            ``True`` when recording is in progress, ``False`` otherwise.
        """
        self._recording = active
        logger.debug("Recording state set to %s", active)
        self._refresh()

    def set_captions_visible(self, visible: bool) -> None:
        """Update the menu to reflect caption overlay visibility.

        Swaps the menu label between "Show Captions" and "Hide Captions".

        Parameters
        ----------
        visible:
            ``True`` when captions are currently visible.
        """
        self._captions_visible = visible
        logger.debug("Captions visible set to %s", visible)
        self._refresh()

    def set_batch_running(self, running: bool) -> None:
        """Update tray to reflect whether batch re-transcription is active.

        When batch is running, "Start Recording" is disabled.

        Parameters
        ----------
        running:
            ``True`` when batch re-transcription is in progress.
        """
        self._batch_running = running
        logger.debug("Batch running set to %s", running)
        self._refresh()

    def set_mic_device(self, device_name: Optional[str]) -> None:
        """Update the currently selected microphone device.

        Changes the checkmark in the "Set Microphone" submenu.

        Parameters
        ----------
        device_name:
            Device name string, or ``None`` for system default.
        """
        self._current_mic = device_name
        self._refresh()

    def set_loopback_devices(self, device_names: list[str]) -> None:
        """Update the list of enabled loopback devices.

        Changes the checkmarks in the "Loopback Devices" submenu.

        Parameters
        ----------
        device_names:
            List of device name strings.  Empty list means system default.
        """
        self._current_loopbacks = list(device_names)
        self._refresh()

    def set_status(self, text: Optional[str]) -> None:
        """Show or clear a status line at the top of the tray menu.

        ``text`` also becomes the icon's hover tooltip so users see the
        current state without opening the menu.  Pass ``None`` to clear.

        Safe to call before :meth:`run` — the value is picked up when
        the icon is constructed.
        """
        self._status_text = text
        logger.debug("Tray status set to %r", text)
        if self._icon is not None:
            try:
                self._icon.title = (
                    f"Tachyon Transcripts — {text}" if text else "Tachyon Transcripts"
                )
            except Exception:
                logger.warning("Tray title update failed", exc_info=True)
        self._refresh()

    def set_model_ready(self, ready: bool) -> None:
        """Toggle whether the transcription model has finished loading.

        While ``False``, the "Start Recording" menu item is disabled so
        the user can't trigger a recording before the model is ready.
        """
        self._model_ready = ready
        logger.debug("Model ready set to %s", ready)
        self._refresh()

    def set_last_session_time(self, dt: Optional[datetime]) -> None:
        """Update the last session timestamp shown in the menu.

        Parameters
        ----------
        dt:
            Datetime of the last recording session, or None.
        """
        self._last_session_time = dt
        self._refresh()

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def notify(self, title: str, message: str) -> None:
        """Display a Windows notification balloon via pystray.

        Does nothing if the tray icon has not been started yet.

        Parameters
        ----------
        title:
            Notification title.
        message:
            Notification body text.
        """
        if self._icon is None:
            logger.warning("notify() called before tray icon started — ignored")
            return
        logger.debug("Notification: %s — %s", title, message)
        try:
            self._icon.notify(message, title=title)
        except Exception:
            logger.warning("Tray notification failed", exc_info=True)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the pystray icon event loop (blocking).

        This method blocks the calling thread until :meth:`stop` is
        called.  It should be run in a dedicated daemon thread::

            threading.Thread(target=tray.run, daemon=True).start()
        """
        self._icon = pystray.Icon(
            name="tachyon_transcripts",
            icon=self._create_icon(recording=False),
            title="Tachyon Transcripts",
            menu=self._build_menu(),
        )
        logger.info("System tray icon starting")
        self._icon.run()

    def stop(self) -> None:
        """Stop the pystray icon and unblock :meth:`run`.

        Safe to call from any thread.  Does nothing if the icon is
        already stopped or was never started.
        """
        if self._icon is not None:
            logger.info("System tray icon stopping")
            self._icon.stop()
            self._icon = None

"""Native Win32 global hotkey listener.

Drop-in replacement for the bits of the ``keyboard`` library we used:
register one global hotkey, run a callback when it fires, unregister
on shutdown.

Why not ``keyboard``?  That library installs a low-level keyboard hook
(``WH_KEYBOARD_LL``) that intercepts every keystroke system-wide.  It
is unmaintained (last meaningful release 2019), is a chronic AV-flag
trigger because the same Windows API is used by keyloggers, and is
massive overkill for a single hotkey.

``RegisterHotKey`` is the native Win32 API for "tell me when this
specific key combo is pressed."  No global hook, no keystroke
interception -- just one event when the user actually presses the
combo.  AV products do not flag it.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import threading
from typing import Callable, Optional


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Win32 constants
# ---------------------------------------------------------------------------

MOD_ALT:     int = 0x0001
MOD_CONTROL: int = 0x0002
MOD_SHIFT:   int = 0x0004
MOD_WIN:     int = 0x0008
MOD_NOREPEAT: int = 0x4000  # don't fire repeatedly while held

WM_HOTKEY: int = 0x0312
WM_QUIT:   int = 0x0012

_USER32 = ctypes.windll.user32


# Modifier-name -> MOD_* mask.  Lowercased keys.  Includes the common
# aliases users may have configured.
_MODIFIER_MAP: dict[str, int] = {
    "ctrl":     MOD_CONTROL,
    "control":  MOD_CONTROL,
    "alt":      MOD_ALT,
    "shift":    MOD_SHIFT,
    "win":      MOD_WIN,
    "windows":  MOD_WIN,
    "super":    MOD_WIN,
    "cmd":      MOD_WIN,
}


# Named-key -> Win32 virtual-key code.  Lowercased keys.  Letters and
# digits are handled directly via ord(), so this only covers the keys
# that don't have a clean ASCII mapping.
_NAMED_KEY_MAP: dict[str, int] = {
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "space": 0x20, "spacebar": 0x20,
    "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B,
    "tab": 0x09,
    "backspace": 0x08,
    "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D, "ins": 0x2D,
    "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pgup": 0x21,
    "pagedown": 0x22, "pgdn": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """Parse a hotkey string like ``"ctrl+shift+t"`` into ``(modifiers, vk)``.

    Tokens are split on ``+``, lowercased, and stripped.  The last token
    is the key; preceding tokens are modifiers.

    Raises:
        ValueError: If the spec is empty, lacks a key, or contains an
            unknown modifier or key name.
    """
    if not spec or not spec.strip():
        raise ValueError("empty hotkey spec")

    tokens = [t.strip().lower() for t in spec.split("+") if t.strip()]
    if not tokens:
        raise ValueError(f"hotkey spec has no tokens: {spec!r}")

    *modifier_tokens, key_token = tokens

    modifiers = 0
    for tok in modifier_tokens:
        mask = _MODIFIER_MAP.get(tok)
        if mask is None:
            raise ValueError(f"unknown modifier {tok!r} in hotkey {spec!r}")
        modifiers |= mask

    # Key: single letter / digit -> ord(), else look up named key.
    if len(key_token) == 1 and key_token.isalnum():
        vk = ord(key_token.upper())
    else:
        vk = _NAMED_KEY_MAP.get(key_token, 0)
        if vk == 0:
            raise ValueError(
                f"unknown key {key_token!r} in hotkey {spec!r}",
            )

    return modifiers, vk


# ---------------------------------------------------------------------------
# HotkeyListener
# ---------------------------------------------------------------------------

class HotkeyListener:
    """Register a single global hotkey and fire a callback when pressed.

    The listener owns a daemon thread that runs a Win32 message loop.
    ``RegisterHotKey`` is thread-affine -- the registering thread is the
    one that receives ``WM_HOTKEY`` messages -- so the loop and the
    registration both live on that thread.

    Parameters
    ----------
    spec:
        Hotkey string like ``"ctrl+shift+t"``.  Parsed via
        :func:`parse_hotkey`.
    callback:
        Zero-argument callable invoked from the listener thread when
        the hotkey fires.  Keep it short or hand off to another thread
        / ``root.after()`` immediately -- blocking it stalls future
        hotkey delivery.

    Usage::

        listener = HotkeyListener("ctrl+shift+t", my_callback)
        listener.start()         # may raise OSError on registration failure
        ...
        listener.stop()

    Lifecycle:
        - ``start()`` blocks until the message loop is up and the hotkey
          is registered (or fails).
        - ``stop()`` posts WM_QUIT to the listener thread, unregisters
          the hotkey, and joins.
    """

    _HOTKEY_ID: int = 1  # arbitrary; we only register one hotkey

    def __init__(self, spec: str, callback: Callable[[], None]) -> None:
        self._spec: str = spec
        self._callback: Callable[[], None] = callback
        self._thread: Optional[threading.Thread] = None
        self._thread_id: int = 0
        self._ready: threading.Event = threading.Event()
        self._registration_error: Optional[BaseException] = None

        # Parse early so a bad spec fails fast at construction time.
        self._modifiers, self._vk = parse_hotkey(spec)

    def start(self) -> None:
        """Start the listener thread.

        Raises:
            OSError: If ``RegisterHotKey`` fails (e.g. the combo is
                already registered by another process).
            RuntimeError: If the thread fails to start.
        """
        if self._thread is not None and self._thread.is_alive():
            return

        self._ready.clear()
        self._registration_error = None
        self._thread = threading.Thread(
            target=self._run, name="HotkeyListener", daemon=True,
        )
        self._thread.start()

        # Block until the thread either registered successfully or failed.
        self._ready.wait(timeout=5.0)
        if self._registration_error is not None:
            err = self._registration_error
            self._registration_error = None
            raise err

    def stop(self) -> None:
        """Tear down the listener thread.

        Posts ``WM_QUIT`` to the thread's message queue so the loop
        exits, then joins.  Safe to call from any thread, including
        repeatedly.
        """
        if self._thread is None:
            return
        if self._thread_id:
            # PostThreadMessageW returns BOOL; non-zero on success.
            _USER32.PostThreadMessageW(
                wintypes.DWORD(self._thread_id),
                wintypes.UINT(WM_QUIT),
                wintypes.WPARAM(0),
                wintypes.LPARAM(0),
            )
        self._thread.join(timeout=2.0)
        if self._thread.is_alive():
            logger.warning(
                "Hotkey listener thread did not exit within timeout.",
            )
        self._thread = None
        self._thread_id = 0

    # ------------------------------------------------------------------
    # Thread body
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Listener thread body: register, pump messages, unregister."""
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()

        # RegisterHotKey(hwnd=NULL, id, modifiers, vk).  hwnd=NULL means
        # WM_HOTKEY is posted to this thread's message queue rather than
        # to a window's WndProc -- which is exactly what we want.
        ok = _USER32.RegisterHotKey(
            None,
            self._HOTKEY_ID,
            self._modifiers | MOD_NOREPEAT,
            self._vk,
        )
        if not ok:
            err = ctypes.WinError()
            logger.warning(
                "RegisterHotKey failed for %r: %s", self._spec, err,
            )
            self._registration_error = err
            self._ready.set()
            return

        self._ready.set()
        logger.info("Global hotkey '%s' registered (Win32 RegisterHotKey).", self._spec)

        # Message pump.  GetMessageW returns 0 on WM_QUIT, -1 on error,
        # otherwise non-zero.
        msg = wintypes.MSG()
        try:
            while True:
                ret = _USER32.GetMessageW(
                    ctypes.byref(msg), None, 0, 0,
                )
                if ret == 0:
                    # WM_QUIT
                    break
                if ret == -1:
                    logger.warning(
                        "GetMessageW error in hotkey listener; exiting loop.",
                    )
                    break
                if msg.message == WM_HOTKEY and msg.wParam == self._HOTKEY_ID:
                    try:
                        self._callback()
                    except Exception:
                        logger.exception(
                            "Hotkey callback raised; continuing.",
                        )
                    continue
                # Anything else: dispatch normally (no-op for us, but
                # keeps the loop friendly to any future window-based
                # extensions).
                _USER32.TranslateMessage(ctypes.byref(msg))
                _USER32.DispatchMessageW(ctypes.byref(msg))
        finally:
            _USER32.UnregisterHotKey(None, self._HOTKEY_ID)
            logger.info("Global hotkey '%s' unregistered.", self._spec)

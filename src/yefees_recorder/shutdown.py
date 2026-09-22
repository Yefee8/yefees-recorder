"""Finish the recording cleanly when the terminal goes away.

Ctrl+C already raises KeyboardInterrupt, which the record loop catches. Closing
the window does not:

- on POSIX the process gets SIGHUP, and `kill` sends SIGTERM;
- on Windows the console sends CTRL_CLOSE_EVENT, which Python does not turn
  into a signal at all, so nothing in a normal `except` can see it.

Without handling these the ffmpeg child is cut off mid-write and the recording
is left unfinalised. Everything here funnels into one idempotent callback, so
the normal path and an abrupt exit finish the file exactly the same way.

Windows gives a console-close handler only a few seconds before killing the
process anyway, so the work done there has to be the short kind: telling ffmpeg
to stop and letting it close its own file.
"""

from __future__ import annotations

import signal
import sys
import threading
from typing import Callable

# Console events Windows sends when the window is closed or the user logs out.
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6
CLOSING_EVENTS = (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT)

# A console handler must outlive this call or it is garbage collected and the
# process crashes when Windows tries to use it.
_HANDLERS: list = []


def once(action: Callable[[], None]) -> Callable[[], None]:
    """Wrap `action` so it runs at most once, whoever calls it first.

    The signal handler and the normal end of a recording both want to finish
    the file, and a second attempt would fail on an already-cleaned workspace.
    """
    done = threading.Lock()
    state = {"ran": False}

    def run() -> None:
        with done:
            if state["ran"]:
                return
            state["ran"] = True
        action()

    return run


def _install_signals(finish: Callable[[], None]) -> None:
    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            signal.signal(number, lambda *_: _finish_and_exit(finish))
        except (ValueError, OSError):
            pass  # not the main thread, or the platform will not allow it


def _finish_and_exit(finish: Callable[[], None]) -> None:
    finish()
    sys.exit(0)


def _install_console_handler(finish: Callable[[], None]) -> None:
    """Catch the Windows console close button, which sends no signal."""
    import ctypes
    from ctypes import wintypes

    prototype = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    def handler(event: int) -> bool:
        if event not in CLOSING_EVENTS:
            return False  # let Ctrl+C and Ctrl+Break take their usual path
        finish()
        return True

    callback = prototype(handler)
    _HANDLERS.append(callback)
    try:
        ctypes.windll.kernel32.SetConsoleCtrlHandler(callback, True)
    except (AttributeError, OSError):
        pass  # no console attached; nothing to catch


def install(action: Callable[[], None]) -> Callable[[], None]:
    """Run `action` on an abrupt exit, and return it for the normal path too."""
    finish = once(action)
    _install_signals(finish)
    if sys.platform == "win32":
        _install_console_handler(finish)
    return finish

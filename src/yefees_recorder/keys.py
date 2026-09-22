"""Non-blocking single-key reads from the terminal.

The plan called for `blessed`, but its `inkey()` does nothing on Windows: it
returns an empty key immediately and ignores the timeout, so a hotkey loop
built on it would spin at full speed and never see a keypress. The standard
library covers all three platforms in about the same amount of code, so there
is no dependency here.

Keys come back as a single character, or one of the names in `NAMED` for the
keys that arrive as escape sequences.
"""

from __future__ import annotations

import contextlib
import sys
import time

WINDOWS = sys.platform == "win32"

if WINDOWS:
    import msvcrt
else:
    import select
    import termios
    import tty

UP, DOWN, LEFT, RIGHT = "up", "down", "left", "right"
ENTER, ESC, BACKSPACE = "enter", "esc", "backspace"
NAMED = {UP, DOWN, LEFT, RIGHT, ENTER, ESC, BACKSPACE}

# Windows sends a two-value sequence for arrows: a prefix, then the code below.
_WINDOWS_ARROWS = {"H": UP, "P": DOWN, "K": LEFT, "M": RIGHT}
# Everywhere else they arrive as ESC [ A .. D.
_ANSI_ARROWS = {"A": UP, "B": DOWN, "C": RIGHT, "D": LEFT}


def interactive() -> bool:
    """Whether there is a terminal to read keys from at all."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


@contextlib.contextmanager
def raw_mode():
    """Deliver keystrokes one at a time instead of a line at a time.

    Ctrl+C keeps working: cbreak leaves signal handling on, and the Windows
    console raises KeyboardInterrupt regardless.
    """
    if WINDOWS or not interactive():
        yield  # the Windows console is already unbuffered; nothing to restore
        return
    descriptor = sys.stdin.fileno()
    saved = termios.tcgetattr(descriptor)
    try:
        tty.setcbreak(descriptor)
        yield
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)


def _normalise(char: str) -> str:
    if char in ("\r", "\n"):
        return ENTER
    if char in ("\x7f", "\b"):
        return BACKSPACE
    return char


def _read_windows(timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    while True:
        if msvcrt.kbhit():
            char = msvcrt.getwch()
            if char in ("\x00", "\xe0"):  # arrow and function keys send two values
                return _WINDOWS_ARROWS.get(msvcrt.getwch())
            if char == "\x1b":
                return ESC
            return _normalise(char)
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.02)  # polling, but idle — kbhit() does not block


def _read_posix(timeout: float) -> str | None:
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if not ready:
        return None
    char = sys.stdin.read(1)
    if char != "\x1b":
        return _normalise(char)
    # Escape on its own, or the start of a sequence: a bare Esc has nothing
    # queued behind it, so a zero timeout tells the two apart.
    if not select.select([sys.stdin], [], [], 0.05)[0]:
        return ESC
    if sys.stdin.read(1) != "[":
        return ESC
    return _ANSI_ARROWS.get(sys.stdin.read(1), ESC)


def read_key(timeout: float) -> str | None:
    """One keypress, or None if `timeout` seconds pass without one.

    Arrows and Enter come back as the names above; everything else as itself.
    """
    if not interactive():
        time.sleep(timeout)
        return None
    return _read_windows(timeout) if WINDOWS else _read_posix(timeout)

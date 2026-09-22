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
import os
import sys
import time

WINDOWS = sys.platform == "win32"

if WINDOWS:
    import msvcrt
else:
    import select
    import termios
    import tty

# How often to look for a keypress on Windows, where there is nothing to block
# on. Small enough not to be felt, large enough not to spin a core.
POLL_SECONDS = 0.003

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


def _terminal_state() -> tuple[int, list] | None:
    """stdin's descriptor and its current settings, or None when it has neither.

    isatty() can say yes for a stream that has no descriptor behind it - a
    captured stdin is exactly that - and termios refuses anything that is not a
    real tty. Either case leaves nothing to switch, so raw_mode does nothing
    rather than raising in the middle of a recording.
    """
    if WINDOWS or not interactive():
        return None
    try:
        descriptor = sys.stdin.fileno()
        return descriptor, termios.tcgetattr(descriptor)
    except (OSError, ValueError, termios.error):
        return None


@contextlib.contextmanager
def raw_mode():
    """Deliver keystrokes one at a time instead of a line at a time.

    Ctrl+C keeps working: cbreak leaves signal handling on, and the Windows
    console raises KeyboardInterrupt regardless.
    """
    state = _terminal_state()
    if state is None:
        yield  # the Windows console is already unbuffered; nothing to restore
        return
    descriptor, saved = state
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
        # Windows sleeps overshoot badly - asking for 20 ms measures ~62 ms -
        # and that lag is what makes a held arrow key feel like it is dragging.
        time.sleep(POLL_SECONDS)


def _read_char(descriptor: int) -> str:
    """One character straight off the descriptor, multi-byte ones included.

    `os.read` and not `sys.stdin.read`: the buffered stream pulls a whole chunk
    out of the kernel and hands back one character, leaving the rest where
    select cannot see it - which is what made every arrow key read as a bare
    Esc and put the next keypress out of step behind it.
    """
    first = os.read(descriptor, 1)
    if not first:
        return ""
    # A UTF-8 lead byte says how many continuations follow, and a terminal
    # sends them in the same write, so they are already waiting.
    lead = first[0]
    expected = 4 if lead >= 0xF0 else 3 if lead >= 0xE0 else 2 if lead >= 0xC0 else 1
    data = first
    while len(data) < expected:
        more = os.read(descriptor, expected - len(data))
        if not more:
            break
        data += more
    return data.decode("utf-8", "replace")


def _read_posix(timeout: float) -> str | None:
    descriptor = sys.stdin.fileno()
    ready, _, _ = select.select([descriptor], [], [], timeout)
    if not ready:
        return None
    char = _read_char(descriptor)
    if not char:
        # End of input: the terminal went away and nothing will ever arrive
        # again, but select keeps reporting the descriptor ready. Waiting out
        # the timeout is what stops that from becoming a busy spin - the very
        # thing blessed was rejected for.
        time.sleep(timeout)
        return None
    if char != "\x1b":
        return _normalise(char)
    # Escape on its own, or the start of a sequence: a bare Esc has nothing
    # queued behind it, so a zero timeout tells the two apart.
    if not select.select([descriptor], [], [], 0.05)[0]:
        return ESC
    if _read_char(descriptor) != "[":
        return ESC
    return _ANSI_ARROWS.get(_read_char(descriptor), ESC)


def read_key(timeout: float) -> str | None:
    """One keypress, or None if `timeout` seconds pass without one.

    Arrows and Enter come back as the names above; everything else as itself.
    """
    if not interactive():
        time.sleep(timeout)
        return None
    return _read_windows(timeout) if WINDOWS else _read_posix(timeout)

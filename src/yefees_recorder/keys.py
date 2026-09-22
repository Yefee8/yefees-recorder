"""Non-blocking single-key reads from the terminal.

The plan called for `blessed`, but its `inkey()` does nothing on Windows: it
returns an empty key immediately and ignores the timeout, so a hotkey loop
built on it would spin at full speed and never see a keypress. The standard
library covers all three platforms in about the same amount of code, so there
is no dependency here.
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


def read_key(timeout: float) -> str | None:
    """One keypress, or None if `timeout` seconds pass without one."""
    if not interactive():
        time.sleep(timeout)
        return None
    if WINDOWS:
        deadline = time.monotonic() + timeout
        while True:
            if msvcrt.kbhit():
                key = msvcrt.getwch()
                if key in ("\x00", "\xe0"):  # function/arrow keys send two values
                    msvcrt.getwch()
                    return None
                return key
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.02)  # polling, but idle — kbhit() does not block
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    return sys.stdin.read(1) if ready else None

"""Hotkey handling.

`blessed` was the plan's choice and is deliberately not used: on Windows its
inkey() returns immediately with no key and ignores the timeout, which would
turn the wait loop into a busy spin that never sees a keypress. These tests
pin the behaviour the stdlib replacement has to keep.
"""

import io
import os
import sys
import time

import pytest

from yefees_recorder import cli, keys


def test_read_key_honours_its_timeout_when_nothing_is_typed():
    start = time.monotonic()
    with keys.raw_mode():
        assert keys.read_key(0.3) is None
    assert time.monotonic() - start >= 0.25, "returning early would busy-spin the wait loop"


def test_read_key_honours_its_timeout_when_the_input_has_ended(monkeypatch):
    """A terminal that goes away leaves stdin readable and empty for good, so
    without this the wait loop spins a core - measured at 380,000 calls a second
    against the five the timeout allows."""
    if keys.WINDOWS:
        pytest.skip("the Windows console has no end of input to reach")

    class Ended:
        def isatty(self):
            return True

        def fileno(self):
            return self._read

        def read(self, count):
            return ""

    ended = Ended()
    ended._read, writer = os.pipe()
    os.close(writer)  # nothing will ever be written, and the reader is at EOF
    monkeypatch.setattr(keys, "interactive", lambda: True)
    monkeypatch.setattr(sys, "stdin", ended)
    start = time.monotonic()
    assert keys.read_key(0.3) is None
    assert time.monotonic() - start >= 0.25
    os.close(ended._read)


@pytest.mark.skipif(keys.WINDOWS, reason="the Windows console has its own reader")
def test_keys_decode_off_a_real_terminal():
    """Every menu test replaces read_key, so nothing exercised the decoding.

    A buffered read pulls the whole escape sequence out of the kernel and hands
    back one character, leaving select blind to the rest: arrows came back as a
    bare Esc and the leftovers surfaced as the next keypress, so the stream
    stayed out of step for good.
    """
    import pty

    master, slave = pty.openpty()
    real_stdin = sys.stdin
    sys.stdin = os.fdopen(slave, "r")
    try:
        with keys.raw_mode():
            for written, expected in [
                (b"\x1b[A", keys.UP), (b"\x1b[B", keys.DOWN),
                (b"\x1b[D", keys.LEFT), (b"\x1b[C", keys.RIGHT),
                (b"\r", keys.ENTER), (b"\x7f", keys.BACKSPACE),
                (b"q", "q"), (b" ", " "),
                ("\u00f6".encode(), "\u00f6"),   # one keypress, two bytes
                (b"\x1b", keys.ESC),            # a bare Esc, not a sequence
            ]:
                os.write(master, written)
                time.sleep(0.05)
                assert keys.read_key(0.5) == expected, f"after writing {written!r}"
    finally:
        sys.stdin.close()
        sys.stdin = real_stdin
        os.close(master)


class NoDescriptor:
    """A stdin that claims to be a terminal but has no file descriptor."""

    def isatty(self):
        return True

    def fileno(self):
        raise io.UnsupportedOperation("redirected stdin is pseudofile, has no fileno()")


def test_raw_mode_is_a_no_op_when_stdin_has_no_descriptor(monkeypatch):
    """A captured stdin is a terminal by isatty() and has no descriptor at all.

    Windows never hit this because raw_mode short-circuits before it looks at
    stdin; on POSIX it took out every test that goes through the wait loop.
    """
    monkeypatch.setattr(keys, "interactive", lambda: True)
    monkeypatch.setattr(sys, "stdin", NoDescriptor())
    with keys.raw_mode():
        pass


def test_raw_mode_restores_itself_even_if_the_body_raises():
    with pytest.raises(ZeroDivisionError):
        with keys.raw_mode():
            1 / 0
    with keys.raw_mode():  # still usable afterwards
        pass


class FakeBackend:
    # Part of the backend contract the wait loop reads: whether the recorder
    # enforces the duration itself, and whether it has stopped on its own.
    limits_duration = False
    capture_ended = False

    def check_audio(self):
        """Asked each time round the loop; a fake never has anything to say."""

    def __init__(self):
        self.calls = []

    def pause(self):
        self.calls.append("pause")

    def resume(self):
        self.calls.append("resume")


def scripted(*presses):
    """Feed keys to the wait loop, then stop rather than hang if it runs dry."""
    remaining = iter(presses)

    def read_key(timeout):
        return next(remaining, "q")

    return read_key


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setattr(keys, "interactive", lambda: True)
    return FakeBackend()


def test_p_toggles_pause_and_resume(monkeypatch, backend):
    monkeypatch.setattr(keys, "read_key", scripted("p", "p", "p", "q"))
    cli._run_until_stopped(backend, 0)
    assert backend.calls == ["pause", "resume", "pause"]


def test_space_pauses_too(monkeypatch, backend):
    monkeypatch.setattr(keys, "read_key", scripted(" ", "q"))
    cli._run_until_stopped(backend, 0)
    assert backend.calls == ["pause"]


def test_q_stops_without_touching_the_backend(monkeypatch, backend):
    monkeypatch.setattr(keys, "read_key", scripted("q"))
    cli._run_until_stopped(backend, 0)
    assert backend.calls == []


def test_unrelated_keys_are_ignored(monkeypatch, backend):
    monkeypatch.setattr(keys, "read_key", scripted("x", "1", "\n", "q"))
    cli._run_until_stopped(backend, 0)
    assert backend.calls == []


def test_a_backend_complaint_does_not_end_the_recording(monkeypatch, backend):
    """A refused pause should be reported, not crash out mid-recording."""

    def refuse():
        raise RuntimeError("not recording")

    backend.pause = refuse
    monkeypatch.setattr(keys, "read_key", scripted("p", "q"))
    cli._run_until_stopped(backend, 0)  # must return normally


def test_duration_ends_the_loop_without_any_keypress(monkeypatch, backend):
    monkeypatch.setattr(keys, "read_key", lambda timeout: None)
    start = time.monotonic()
    cli._run_until_stopped(backend, 0.4)
    assert 0.3 < time.monotonic() - start < 2.0


def test_a_recorder_that_stops_itself_ends_the_loop(monkeypatch, backend):
    """A backend counting the duration itself finishes later than any clock here
    would, so the loop has to watch for it rather than time it."""
    backend.capture_ended = True
    monkeypatch.setattr(keys, "read_key", lambda timeout: pytest.fail("should not wait for a key"))
    cli._run_until_stopped(backend, 30)  # must return at once, not in 30s


def test_a_self_timing_recorder_is_given_grace_past_its_duration(monkeypatch, backend):
    """Stopping it on the clock here would cut off exactly the startup time the
    -t is there to recover."""
    backend.limits_duration = True
    deadlines = []
    monkeypatch.setattr(cli, "_watch_for_keys",
                        lambda b, deadline, reported: deadlines.append(deadline))
    start = time.monotonic()
    cli._run_until_stopped(backend, 5)
    assert deadlines[0] - start > 5 + cli.STARTUP_GRACE - 1


def test_a_problem_found_mid_recording_is_reported_once(monkeypatch, backend):
    """A device can take over a second to refuse, so the news arrives after the
    recording has started - and then it must not repeat every 0.2s."""
    said = []
    monkeypatch.setattr(cli.console, "print", lambda message, *a, **k: said.append(str(message)))
    backend.audio_error = "the device would not open"
    monkeypatch.setattr(keys, "read_key", scripted("x", "x", "x", "q"))
    cli._run_until_stopped(backend, 0)
    assert sum("would not open" in line for line in said) == 1


def test_ctrl_c_stops_cleanly(monkeypatch, backend):
    def interrupt(timeout):
        raise KeyboardInterrupt

    monkeypatch.setattr(keys, "read_key", interrupt)
    cli._run_until_stopped(backend, 0)  # must be swallowed so the file is finalised

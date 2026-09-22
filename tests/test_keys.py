"""Hotkey handling.

`blessed` was the plan's choice and is deliberately not used: on Windows its
inkey() returns immediately with no key and ignores the timeout, which would
turn the wait loop into a busy spin that never sees a keypress. These tests
pin the behaviour the stdlib replacement has to keep.
"""

import io
import sys
import time

import pytest

from yefees_recorder import cli, keys


def test_read_key_honours_its_timeout_when_nothing_is_typed():
    start = time.monotonic()
    with keys.raw_mode():
        assert keys.read_key(0.3) is None
    assert time.monotonic() - start >= 0.25, "returning early would busy-spin the wait loop"


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
    monkeypatch.setattr(cli, "_watch_for_keys", lambda b, deadline: deadlines.append(deadline))
    start = time.monotonic()
    cli._run_until_stopped(backend, 5)
    assert deadlines[0] - start > 5 + cli.STARTUP_GRACE - 1


def test_ctrl_c_stops_cleanly(monkeypatch, backend):
    def interrupt(timeout):
        raise KeyboardInterrupt

    monkeypatch.setattr(keys, "read_key", interrupt)
    cli._run_until_stopped(backend, 0)  # must be swallowed so the file is finalised

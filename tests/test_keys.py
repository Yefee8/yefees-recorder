"""Hotkey handling.

`blessed` was the plan's choice and is deliberately not used: on Windows its
inkey() returns immediately with no key and ignores the timeout, which would
turn the wait loop into a busy spin that never sees a keypress. These tests
pin the behaviour the stdlib replacement has to keep.
"""

import time

import pytest

from yefees_recorder import cli, keys


def test_read_key_honours_its_timeout_when_nothing_is_typed():
    start = time.monotonic()
    with keys.raw_mode():
        assert keys.read_key(0.3) is None
    assert time.monotonic() - start >= 0.25, "returning early would busy-spin the wait loop"


def test_raw_mode_restores_itself_even_if_the_body_raises():
    with pytest.raises(ZeroDivisionError):
        with keys.raw_mode():
            1 / 0
    with keys.raw_mode():  # still usable afterwards
        pass


class FakeBackend:
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


def test_ctrl_c_stops_cleanly(monkeypatch, backend):
    def interrupt(timeout):
        raise KeyboardInterrupt

    monkeypatch.setattr(keys, "read_key", interrupt)
    cli._run_until_stopped(backend, 0)  # must be swallowed so the file is finalised

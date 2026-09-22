"""Finishing the recording when the terminal goes away.

Verified against a real recording as well: sending CTRL_BREAK to a running
`yefees-recorder record` leaves a playable 5.86s file, where killing the
process outright leaves no file at all.
"""

import signal
import sys
import threading

import pytest

from yefees_recorder import shutdown


class TestRunsOnce:
    def test_a_second_call_does_nothing(self):
        calls = []
        finish = shutdown.once(lambda: calls.append(1))
        finish()
        finish()
        finish()
        assert calls == [1]

    def test_only_one_thread_wins(self):
        """The signal handler runs on its own thread and can race the main one."""
        calls = []
        started = threading.Barrier(8)

        def action():
            calls.append(1)

        finish = shutdown.once(action)

        def racer():
            started.wait()
            finish()

        threads = [threading.Thread(target=racer) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert calls == [1]


class TestInstall:
    def test_it_registers_without_complaining(self, monkeypatch):
        registered = {}
        monkeypatch.setattr(signal, "signal",
                            lambda number, handler: registered.setdefault(number, handler))
        finish = shutdown.install(lambda: None)
        assert callable(finish)
        assert signal.SIGTERM in registered

    def test_a_platform_refusing_a_signal_is_not_fatal(self, monkeypatch):
        def refuse(number, handler):
            raise ValueError("signal only works in main thread")

        monkeypatch.setattr(signal, "signal", refuse)
        assert callable(shutdown.install(lambda: None))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console events")
class TestWindowsConsoleHandler:
    def test_closing_the_window_finishes_the_recording(self, monkeypatch):
        """Windows sends no signal for this, so nothing else would catch it."""
        calls = []
        monkeypatch.setattr(signal, "signal", lambda *a: None)
        shutdown.install(lambda: calls.append("finished"))
        handler = shutdown._HANDLERS[-1]

        assert bool(handler(shutdown.CTRL_CLOSE_EVENT)) is True
        assert calls == ["finished"]

    @pytest.mark.parametrize("event", [0, 1])
    def test_ctrl_c_and_ctrl_break_are_left_alone(self, monkeypatch, event):
        """Those already have a path; claiming them here would double-handle."""
        calls = []
        monkeypatch.setattr(signal, "signal", lambda *a: None)
        shutdown.install(lambda: calls.append("finished"))
        handler = shutdown._HANDLERS[-1]

        assert bool(handler(event)) is False
        assert calls == []

    def test_logoff_and_shutdown_also_finish(self, monkeypatch):
        calls = []
        monkeypatch.setattr(signal, "signal", lambda *a: None)
        shutdown.install(lambda: calls.append("finished"))
        handler = shutdown._HANDLERS[-1]

        assert bool(handler(shutdown.CTRL_SHUTDOWN_EVENT)) is True
        assert calls == ["finished"]


def test_stopping_twice_returns_the_same_file(tmp_path):
    """The handler and the normal path both call stop, and can race."""
    import shutil
    import time

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")

    from yefees_recorder.capture import CaptureBackend

    class ToneBackend(CaptureBackend):
        def video_input_args(self):
            return ["-re", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10"]

    backend = ToneBackend(tmp_path / "out.mp4", fps=10, audio=False)
    backend.start()
    time.sleep(1)
    first = backend.stop()
    second = backend.stop()
    assert first == second
    assert first.exists()

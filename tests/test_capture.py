"""Exercises the segment/pause/concat lifecycle against ffmpeg's synthetic
source, so it runs anywhere without a real screen or audio device."""

import shutil
import subprocess
import time

import pytest

from yefees_recorder.capture import CaptureBackend

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


class FakeBackend(CaptureBackend):
    def video_input_args(self):
        return ["-re", "-f", "lavfi", "-i", f"testsrc=size=320x240:rate={self.fps}"]


def duration_of(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def test_records_a_playable_file(tmp_path):
    backend = FakeBackend(tmp_path / "out.mp4", fps=10, audio=False)
    backend.start()
    assert backend.recording
    time.sleep(2)
    out = backend.stop()
    assert not backend.recording
    assert out.exists()
    assert 1 < duration_of(out) < 3.5


def test_pause_skips_the_gap(tmp_path):
    backend = FakeBackend(tmp_path / "out.mp4", fps=10, audio=False)
    backend.start()
    time.sleep(1.5)
    backend.pause()
    assert not backend.recording
    time.sleep(2)  # this gap must not land in the output
    backend.resume()
    assert backend.recording
    time.sleep(1.5)
    out = backend.stop()
    assert len(backend._segments) == 2
    assert duration_of(out) < 4.5  # ~3s of capture, not ~5s of wall clock


def test_start_twice_is_an_error(tmp_path):
    backend = FakeBackend(tmp_path / "out.mp4", fps=10, audio=False)
    backend.start()
    try:
        with pytest.raises(RuntimeError):
            backend.start()
    finally:
        backend.stop()

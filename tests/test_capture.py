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


def test_a_plain_backend_does_not_limit_its_own_duration(tmp_path):
    """Only a backend whose device is slow to wake up needs ffmpeg to count; for
    the rest the caller still times the recording, exactly as before."""
    backend = FakeBackend(tmp_path / "out.mp4", duration=5, audio=False)
    assert not backend.limits_duration
    assert "-t" not in backend.capture_command(tmp_path / "s.mkv")


def test_start_twice_is_an_error(tmp_path):
    backend = FakeBackend(tmp_path / "out.mp4", fps=10, audio=False)
    backend.start()
    try:
        with pytest.raises(RuntimeError):
            backend.start()
    finally:
        backend.stop()


class TestRegionAndQuality:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("0,0,1920x1080", (0, 0, 1920, 1080)),
            (" 10 , 20 , 800 x 600 ", (10, 20, 800, 600)),
            ("-1920,0,1920x1080", (-1920, 0, 1920, 1080)),  # monitor left of primary
        ],
    )
    def test_parses_regions(self, text, expected):
        from yefees_recorder.capture import parse_region

        assert parse_region(text) == expected

    @pytest.mark.parametrize("text", ["", "1,2,3", "0,0,0x100", "0,0,100x0", "a,b,cxd", "0,0,100*100"])
    def test_rejects_malformed_regions(self, text):
        from yefees_recorder.capture import parse_region

        with pytest.raises(ValueError):
            parse_region(text)

    def test_quality_sets_preset_and_crf(self, tmp_path):
        low = FakeBackend(tmp_path / "o.mp4", quality="low")
        high = FakeBackend(tmp_path / "o.mp4", quality="high")
        assert low.crf > high.crf, "lower quality must mean a higher crf"
        assert "-crf" in low.capture_command(tmp_path / "s.mkv")

    def test_unknown_quality_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="unknown quality"):
            FakeBackend(tmp_path / "o.mp4", quality="cinematic")

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


def make_video(path, seconds):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"testsrc=size=64x48:rate=10:duration={seconds}",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )


def make_wav(path, seconds):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}", str(path)],
        check=True,
    )


def muxed_with_lead(tmp_path, lead, video_seconds, wav_seconds):
    """Mux a video and a wav whose start times are `lead` seconds apart."""
    backend = FakeBackend(tmp_path / "out.mp4", audio=False)
    backend.audio_lead = lambda video, audio: lead
    video, wav = backend.workdir / "seg0.mkv", backend.workdir / "seg0.wav"
    make_video(video, video_seconds)
    make_wav(wav, wav_seconds)
    merged = backend._mux(0, video, wav)
    return duration_of(merged), audio_duration_of(merged)


def audio_duration_of(path):
    """Where the audio track ends. Matroska carries no per-stream duration, so
    this is the end of its last packet."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "packet=pts_time,duration_time", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    start, length = (float(x) for x in out.stdout.split()[-1].split(","))
    return start + length


def test_a_wav_older_than_the_first_frame_has_its_head_removed(tmp_path):
    """The recording device can open before the capture one, and then the sound
    at the top of the wav happened before anything was on screen."""
    video, audio = muxed_with_lead(tmp_path, 0.5, 2.0, 2.5)
    assert abs(audio - video) < 0.15, "the extra half second should be gone"


def test_a_wav_that_starts_late_is_padded_up_to_the_first_frame(tmp_path):
    """The other way round the sound runs ahead of the picture, and without
    this it would run a segment further ahead at every pause."""
    video, audio = muxed_with_lead(tmp_path, -0.5, 2.0, 1.5)
    assert abs(audio - video) < 0.15, "the missing half second should be silence"


def test_start_twice_is_an_error(tmp_path):
    backend = FakeBackend(tmp_path / "out.mp4", fps=10, audio=False)
    backend.start()
    # stop() refuses a recording with nothing in it, and the cleanup below is
    # not what this test is about. A matroska file stays empty on disk until
    # ffmpeg flushes, so there is nothing to watch for - only time to give it.
    time.sleep(2)
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

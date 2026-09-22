"""macOS backend wiring, tested against real `-list_devices` output. Nothing
here needs a Mac; capturing an actual screen is still unverified."""

import subprocess
from pathlib import Path

import pytest

from yefees_recorder import macos
from yefees_recorder.macos import (
    MacBackend,
    find_loopback_device,
    find_screen_device,
    list_avfoundation_devices,
)

# Verbatim shape of `ffmpeg -f avfoundation -list_devices true -i ""` on stderr.
DEVICE_OUTPUT = """[AVFoundation indev @ 0x7f8e5c004600] AVFoundation video devices:
[AVFoundation indev @ 0x7f8e5c004600] [0] FaceTime HD Camera
[AVFoundation indev @ 0x7f8e5c004600] [1] Capture screen 0
[AVFoundation indev @ 0x7f8e5c004600] [2] Capture screen 1
[AVFoundation indev @ 0x7f8e5c004600] AVFoundation audio devices:
[AVFoundation indev @ 0x7f8e5c004600] [0] Built-in Microphone
[AVFoundation indev @ 0x7f8e5c004600] [1] BlackHole 2ch
: Input/output error
"""


@pytest.fixture
def listed(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", DEVICE_OUTPUT),
    )


def test_parses_devices_ignoring_the_log_prefix(listed):
    video, audio = list_avfoundation_devices()
    assert video == {0: "FaceTime HD Camera", 1: "Capture screen 0", 2: "Capture screen 1"}
    assert audio == {0: "Built-in Microphone", 1: "BlackHole 2ch"}


def test_picks_the_first_screen_and_the_loopback_device(listed):
    video, audio = list_avfoundation_devices()
    assert find_screen_device(video) == 1   # not the FaceTime camera at 0
    assert find_loopback_device(audio) == 1  # not the microphone at 0


def test_no_loopback_device_means_no_camera_or_mic_is_used():
    assert find_loopback_device({0: "Built-in Microphone"}) is None
    assert find_screen_device({0: "FaceTime HD Camera"}) is None


def test_records_screen_and_loopback(listed, monkeypatch, tmp_path):
    monkeypatch.setattr(macos, "screen_recording_permitted", lambda: True)
    command = MacBackend(tmp_path / "o.mp4", fps=30).capture_command(tmp_path / "s.mkv")
    assert "avfoundation" in command
    assert "1:none" in command   # screen device, no audio on the video input
    assert "none:1" in command   # loopback as a separate input
    assert "aac" in command


def test_missing_loopback_still_records_video(listed, monkeypatch, tmp_path):
    monkeypatch.setattr(macos, "screen_recording_permitted", lambda: True)
    monkeypatch.setattr(macos, "find_loopback_device", lambda devices: None)
    backend = MacBackend(tmp_path / "o.mp4")
    command = backend.capture_command(tmp_path / "s.mkv")
    assert "none:" not in " ".join(command)
    assert "blackhole" in backend.audio_error.lower()


def test_denied_permission_fails_loudly_instead_of_recording_black(listed, monkeypatch, tmp_path):
    monkeypatch.setattr(macos, "screen_recording_permitted", lambda: False)
    monkeypatch.setattr(macos, "request_screen_recording", lambda: False)
    with pytest.raises(RuntimeError, match="Screen Recording"):
        MacBackend(tmp_path / "o.mp4").capture_command(tmp_path / "s.mkv")


def test_undetermined_permission_does_not_block_recording(listed, monkeypatch, tmp_path):
    """Non-macOS or an old macOS returns None; that must not be read as denied."""
    monkeypatch.setattr(macos, "screen_recording_permitted", lambda: None)
    assert MacBackend(tmp_path / "o.mp4").capture_command(tmp_path / "s.mkv")

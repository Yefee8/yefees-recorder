"""macOS backend wiring, tested against real `-list_devices` output. Nothing
here needs a Mac; capturing an actual screen is still unverified."""

import platform
import subprocess
from pathlib import Path

import pytest

from yefees_recorder import macos
from yefees_recorder.macos import (
    MacBackend,
    find_loopback_device,
    find_microphone,
    find_screen_device,
    list_avfoundation_devices,
    screen_device_indices,
)

# Verbatim shape of `ffmpeg -f avfoundation -list_devices true -i ""` on stderr,
# taken from ffmpeg 8.1.2 on macOS 14.5 and then given a second screen and a
# loopback device so one sample reaches every branch. The noise around the
# devices is real and is the part worth pinning: the ObjC warning arrives before
# any section, and the "Error opening input" line arrives while the audio
# section is still the current one, so a bracketed number that is not a device
# index has to be rejected rather than read as one.
DEVICE_OUTPUT = """2026-09-22 21:11:40.757 ffmpeg[3102:141277] WARNING: Add NSCameraUseContinuityCameraDeviceType to your Info.plist to use AVCaptureDeviceTypeContinuityCamera.
[AVFoundation indev @ 0x148e11d30] AVFoundation video devices:
[AVFoundation indev @ 0x148e11d30] [0] FaceTime HD Camera
[AVFoundation indev @ 0x148e11d30] [1] Capture screen 0
[AVFoundation indev @ 0x148e11d30] [2] Capture screen 1
[AVFoundation indev @ 0x148e11d30] AVFoundation audio devices:
[AVFoundation indev @ 0x148e11d30] [0] Built-in Microphone
[AVFoundation indev @ 0x148e11d30] [1] BlackHole 2ch
[in#0 @ 0x148e11780] Error opening input: Input/output error
Error opening input file .
Error opening input files: Input/output error
"""

# The same command on a Turkish desktop: macOS translates the camera and the
# microphone, and leaves "Capture screen" alone.
LOCALISED_OUTPUT = """[AVFoundation indev @ 0x148e11d30] AVFoundation video devices:
[AVFoundation indev @ 0x148e11d30] [0] FaceTime HD Kamera
[AVFoundation indev @ 0x148e11d30] [1] Capture screen 0
[AVFoundation indev @ 0x148e11d30] AVFoundation audio devices:
[AVFoundation indev @ 0x148e11d30] [0] MacBook Air Mikrofonu
[in#0 @ 0x148e11780] Error opening input: Input/output error
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


def test_localised_device_names_still_resolve(monkeypatch):
    """Only "Capture screen" is relied on by name, and macOS does not translate it.

    The camera and the microphone are translated, so the microphone has to be
    found by elimination rather than by looking for the word.
    """
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", LOCALISED_OUTPUT),
    )
    video, audio = list_avfoundation_devices()
    assert video == {0: "FaceTime HD Kamera", 1: "Capture screen 0"}
    assert screen_device_indices(video) == [1]  # not the camera at 0
    assert find_microphone(audio) == 0
    assert find_loopback_device(audio) is None


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


@pytest.mark.skipif(platform.system() != "Darwin", reason="needs a real Mac")
def test_parser_matches_this_ffmpeg_builds_real_output():
    """The device list is scraped from stderr, so its format is the fragile part."""
    video, audio = list_avfoundation_devices()
    assert all(isinstance(index, int) for index in {**video, **audio})
    assert not any("indev" in name for name in {**video, **audio}.values()), (
        "the log prefix leaked into a device name"
    )


def test_the_capture_pixel_format_is_named(listed, monkeypatch, tmp_path):
    """No screen device offers the demuxer's yuv420p default, and ffmpeg announces
    the format it falls back to at *error* level - on every recording."""
    monkeypatch.setattr(macos, "screen_recording_permitted", lambda: True)
    args = MacBackend(tmp_path / "o.mp4").video_input_args()
    assert args[args.index("-pixel_format") + 1] == "uyvy422"
    assert args.index("-pixel_format") < args.index("-i"), "it configures the input"


def test_display_maps_past_the_camera_to_the_right_screen(listed, monkeypatch, tmp_path):
    monkeypatch.setattr(macos, "screen_recording_permitted", lambda: True)
    # video devices are camera=0, screens at 1 and 2, so --display 1 is device 2
    command = MacBackend(tmp_path / "o.mp4", display=1).video_input_args()
    assert command[-1] == "2:none"


def test_out_of_range_display_is_reported(listed, monkeypatch, tmp_path):
    monkeypatch.setattr(macos, "screen_recording_permitted", lambda: True)
    with pytest.raises(RuntimeError, match="exposes 2"):
        MacBackend(tmp_path / "o.mp4", display=5).video_input_args()


def test_window_capture_is_refused_with_a_way_forward(listed, tmp_path):
    with pytest.raises(RuntimeError, match="--region"):
        MacBackend(tmp_path / "o.mp4", window="Safari").video_input_args()


def test_region_becomes_a_crop_filter(listed, monkeypatch, tmp_path):
    monkeypatch.setattr(macos, "screen_recording_permitted", lambda: True)
    command = MacBackend(tmp_path / "o.mp4", region=(10, 20, 800, 600)).capture_command(tmp_path / "s.mkv")
    assert "crop=800:600:10:20" in command[command.index("-vf") + 1]


def test_named_audio_device_is_selected(listed, tmp_path):
    backend = MacBackend(tmp_path / "o.mp4", audio_device="built-in")
    assert backend.audio_inputs()[0].args[-1] == "none:0"
    with pytest.raises(RuntimeError, match="No audio device matching"):
        MacBackend(tmp_path / "o.mp4", audio_device="nonexistent").audio_inputs()


def test_mic_picks_the_non_loopback_device(listed, tmp_path):
    """BlackHole is at audio 1 and the built-in mic at 0; they must not swap."""
    backend = MacBackend(tmp_path / "o.mp4", mic=True)
    inputs = backend.audio_inputs()
    assert [i.args[-1] for i in inputs] == ["none:1", "none:0"]


def test_missing_loopback_still_records_the_mic(listed, monkeypatch, tmp_path):
    monkeypatch.setattr(macos, "find_loopback_device", lambda devices: None)
    backend = MacBackend(tmp_path / "o.mp4", mic=True)
    assert [i.args[-1] for i in backend.audio_inputs()] == ["none:0"]
    assert backend.audio_error

"""macOS backend wiring, tested against real `-list_devices` output.

Nothing here needs a Mac - the device list is a recorded sample - but the
backend itself has now been run on one, and most of what these assert is a
measurement from that session rather than a guess. The reasoning is in
CLAUDE.md under "macOS audio" and "Duration on macOS".
"""

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
    # The same audio devices as CoreAudio lists them to PortAudio.
    monkeypatch.setattr(macos, "list_audio_inputs", lambda: {0: "Built-in Microphone", 1: "BlackHole 2ch"})
    monkeypatch.setattr(macos, "microphone_permitted", lambda: True)


def recorded(backend):
    """(label, device index, gain) for each input the backend would record."""
    return [(label, recorder.device, gain) for label, recorder, gain in backend.audio_devices()]


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


def test_the_screen_is_captured_on_its_own(listed, monkeypatch, tmp_path):
    """Measured: a second avfoundation input in this process starves the capture
    session, and eight beeps a second apart come back as two fragments."""
    monkeypatch.setattr(macos, "screen_recording_permitted", lambda: True)
    command = MacBackend(tmp_path / "o.mp4", fps=30).capture_command(tmp_path / "s.mkv")
    assert "avfoundation" in command
    assert "1:none" in command   # screen device, no audio on the video input
    assert not any(argument.startswith("none:") for argument in command)
    assert "-c:a" not in command


def test_the_loopback_is_recorded_beside_it(listed, tmp_path):
    """Through PortAudio, not avfoundation: ffmpeg 8.1's avfoundation throws an
    audio buffer away whenever the next lands before it was read, which is what
    made the sound stutter."""
    backend = MacBackend(tmp_path / "o.mp4", audio_gain=0.5)
    assert recorded(backend) == [("audio", 1, 0.5)]   # BlackHole, not the mic at 0
    assert isinstance(backend.make_audio_recorder(), macos.MixedRecorder)


def test_missing_loopback_still_records_video(listed, monkeypatch, tmp_path):
    monkeypatch.setattr(macos, "screen_recording_permitted", lambda: True)
    monkeypatch.setattr(macos, "find_loopback_device", lambda devices: None)
    backend = MacBackend(tmp_path / "o.mp4")
    assert backend.make_audio_recorder() is None
    assert backend.capture_command(tmp_path / "s.mkv")
    assert "blackhole" in backend.audio_error.lower()


def test_a_refused_microphone_permission_is_explained_not_left_silent(listed, monkeypatch, tmp_path):
    """CoreAudio hands over silence instead of refusing, so nothing fails - which
    is exactly why the user has to be told, for whichever source they asked for."""
    monkeypatch.setattr(macos, "microphone_permitted", lambda: False)
    backend = MacBackend(tmp_path / "o.mp4", mic=True)
    assert backend.make_audio_recorder() is None
    assert "Microphone" in backend.audio_error and "Microphone" in backend.mic_error


def test_undetermined_microphone_permission_still_records(listed, monkeypatch, tmp_path):
    """None means macOS has not asked yet; opening the device is what asks."""
    monkeypatch.setattr(macos, "microphone_permitted", lambda: None)
    assert MacBackend(tmp_path / "o.mp4").make_audio_recorder() is not None


def test_a_missing_audio_library_does_not_stop_the_screen(listed, monkeypatch, tmp_path):
    def missing():
        raise OSError("PortAudio library not found")

    monkeypatch.setattr(macos, "list_audio_inputs", missing)
    backend = MacBackend(tmp_path / "o.mp4")
    assert backend.make_audio_recorder() is None
    assert "PortAudio" in backend.audio_error


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


def test_ffmpeg_is_asked_to_count_the_duration(listed, monkeypatch, tmp_path):
    """avfoundation is 1.3s late with its first frame, and a duration timed from
    launch loses all of it - 5 seconds asked for measured 4.0s of video."""
    monkeypatch.setattr(macos, "screen_recording_permitted", lambda: True)
    command = MacBackend(tmp_path / "o.mp4", duration=5).capture_command(tmp_path / "s.mkv")
    assert command[command.index("-t") + 1] == "5.000"
    assert command.index("-t") == len(command) - 3, "an output option, not an input one"


def test_no_duration_means_no_limit(listed, monkeypatch, tmp_path):
    monkeypatch.setattr(macos, "screen_recording_permitted", lambda: True)
    assert "-t" not in MacBackend(tmp_path / "o.mp4").capture_command(tmp_path / "s.mkv")


def test_a_resumed_segment_asks_for_what_is_left(listed, monkeypatch, tmp_path):
    """-t restarts from zero every time, so handing it the whole duration again
    after a pause would overrun the recording by however much came before."""
    monkeypatch.setattr(macos, "screen_recording_permitted", lambda: True)
    monkeypatch.setattr(macos, "segment_seconds", lambda path: 3.5)
    backend = MacBackend(tmp_path / "o.mp4", duration=10)
    backend._segments.append((tmp_path / "seg0.mkv", None))
    command = backend.capture_command(tmp_path / "seg1.mkv")
    assert command[command.index("-t") + 1] == "6.500"


def test_a_used_up_duration_still_asks_for_something(listed, monkeypatch, tmp_path):
    """-t 0 would leave an empty segment for the concat to choke on."""
    monkeypatch.setattr(macos, "screen_recording_permitted", lambda: True)
    monkeypatch.setattr(macos, "segment_seconds", lambda path: 99.0)
    backend = MacBackend(tmp_path / "o.mp4", duration=10)
    backend._segments.append((tmp_path / "seg0.mkv", None))
    assert float(backend.capture_command(tmp_path / "seg1.mkv")[-2]) > 0


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
    assert recorded(MacBackend(tmp_path / "o.mp4", audio_device="built-in")) == [("audio", 0, 1.0)]
    with pytest.raises(RuntimeError, match="No audio device matching"):
        MacBackend(tmp_path / "o.mp4", audio_device="nonexistent").audio_devices()


def test_mic_picks_the_non_loopback_device(listed, tmp_path):
    """BlackHole is at audio 1 and the built-in mic at 0; they must not swap."""
    backend = MacBackend(tmp_path / "o.mp4", mic=True, mic_gain=2.0)
    assert recorded(backend) == [("audio", 1, 1.0), ("mic", 0, 2.0)]


def test_missing_loopback_still_records_the_mic(listed, monkeypatch, tmp_path):
    monkeypatch.setattr(macos, "find_loopback_device", lambda devices: None)
    backend = MacBackend(tmp_path / "o.mp4", mic=True)
    assert recorded(backend) == [("mic", 0, 1.0)]
    assert backend.audio_error

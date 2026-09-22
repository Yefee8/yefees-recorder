"""Linux backend wiring. These check the command that would be run, which is
testable from any OS; actually capturing an X display is covered by the
Xvfb-based check in CONTRIBUTING-style manual runs (see CLAUDE.md)."""

import signal
import subprocess
from pathlib import Path

import pytest

from yefees_recorder import linux
from yefees_recorder.capture import get_backend
from yefees_recorder.linux import LinuxWaylandBackend, LinuxX11Backend


@pytest.fixture(autouse=True)
def stub_monitor(monkeypatch):
    monkeypatch.setattr(linux, "default_monitor_source", lambda: "sink.monitor")


def test_x11_command_grabs_the_display_and_sink_monitor(monkeypatch, tmp_path):
    monkeypatch.setenv("DISPLAY", ":1")
    command = LinuxX11Backend(tmp_path / "o.mp4", fps=25).capture_command(tmp_path / "seg.mkv")
    assert "x11grab" in command
    assert command[command.index("-i") + 1] == ":1"
    assert "25" in command
    # system audio rides along as a second ffmpeg input, no post-mux needed
    assert "pulse" in command and "sink.monitor" in command
    assert "aac" in command


def test_x11_no_audio_leaves_the_pulse_input_out(tmp_path):
    command = LinuxX11Backend(tmp_path / "o.mp4", audio=False).capture_command(tmp_path / "s.mkv")
    assert "pulse" not in command and "aac" not in command


def test_wayland_uses_wf_recorder_and_stops_on_sigint(monkeypatch, tmp_path):
    monkeypatch.setattr(linux.shutil, "which", lambda name: f"/usr/bin/{name}")
    backend = LinuxWaylandBackend(tmp_path / "o.mp4")
    assert backend.capture_command(tmp_path / "seg.mkv")[0] == "wf-recorder"
    assert backend.stop_signal == signal.SIGINT


def test_wayland_without_wf_recorder_explains_why(monkeypatch, tmp_path):
    monkeypatch.setattr(linux.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="wf-recorder"):
        LinuxWaylandBackend(tmp_path / "o.mp4").capture_command(tmp_path / "seg.mkv")


@pytest.mark.parametrize(
    "session_type, expected",
    [("wayland", LinuxWaylandBackend), ("x11", LinuxX11Backend), ("", LinuxX11Backend)],
)
def test_session_type_picks_the_backend(monkeypatch, tmp_path, session_type, expected):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setenv("XDG_SESSION_TYPE", session_type)
    assert isinstance(get_backend(tmp_path / "o.mp4"), expected)


def test_monitor_source_falls_back_when_pactl_is_missing(monkeypatch):
    monkeypatch.undo()  # drop the stub, exercise the real function
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError))
    assert linux.default_monitor_source() == "default"


MONITORS = """Monitors: 2
 0: +*eDP-1 1920/344x1080/193+0+0  eDP-1
 1: +HDMI-1 2560/521x1440/293+1920+0  HDMI-1
"""


def test_parses_xrandr_including_negative_offsets(monkeypatch):
    monkeypatch.setattr(linux, "_read", lambda cmd: MONITORS)
    assert linux.list_monitors() == [(0, 0, 1920, 1080), (1920, 0, 2560, 1440)]


def test_display_becomes_a_grab_offset_on_the_display_string(monkeypatch, tmp_path):
    monkeypatch.setattr(linux, "list_monitors", lambda: [(0, 0, 1920, 1080), (1920, 0, 2560, 1440)])
    monkeypatch.setenv("DISPLAY", ":0")
    command = LinuxX11Backend(tmp_path / "o.mp4", display=1, audio=False).video_input_args()
    assert command[-1] == ":0+1920,0"
    assert command[command.index("-video_size") + 1] == "2560x1440"


def test_window_uses_window_id_and_not_a_size(monkeypatch, tmp_path):
    monkeypatch.setattr(linux, "list_windows", lambda: [("0x03400003", "Firefox — docs")])
    command = LinuxX11Backend(tmp_path / "o.mp4", window="firefox", audio=False).video_input_args()
    assert command[command.index("-window_id") + 1] == "0x03400003"
    assert "-video_size" not in command


def test_unmatched_window_title_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(linux, "list_windows", lambda: [("0x1", "Firefox")])
    monkeypatch.setattr(linux.shutil, "which", lambda name: "/usr/bin/wmctrl")
    with pytest.raises(RuntimeError, match="No window matching"):
        LinuxX11Backend(tmp_path / "o.mp4", window="gimp", audio=False).video_input_args()


def test_wayland_supports_region_but_not_display_or_window(monkeypatch, tmp_path):
    monkeypatch.setattr(linux.shutil, "which", lambda name: f"/usr/bin/{name}")
    cropped = LinuxWaylandBackend(tmp_path / "o.mp4", region=(10, 20, 800, 600), audio=False)
    assert "-g" in cropped.capture_command(tmp_path / "s.mkv")
    for unsupported in ({"display": 0}, {"window": "Firefox"}):
        with pytest.raises(RuntimeError, match="not supported on Wayland"):
            LinuxWaylandBackend(tmp_path / "o.mp4", **unsupported).capture_command(tmp_path / "s.mkv")


def test_audio_device_overrides_the_default_monitor(tmp_path):
    inputs = LinuxX11Backend(tmp_path / "o.mp4", audio_device="alsa_output.pci.monitor").audio_inputs()
    assert inputs[0].args[-1] == "alsa_output.pci.monitor"


def test_system_audio_and_mic_become_two_mixed_inputs(monkeypatch, tmp_path):
    monkeypatch.setattr(linux, "default_mic_source", lambda: "alsa_input.mic")
    backend = LinuxX11Backend(tmp_path / "o.mp4", mic=True)
    assert len(backend.audio_inputs()) == 2
    command = backend.capture_command(tmp_path / "s.mkv")
    graph = command[command.index("-filter_complex") + 1]
    assert "amix=inputs=2" in graph
    # Measured: without normalize=0 amix scales each input by 1/n, which drops
    # system audio by ~4.4 dB the moment a microphone is added.
    assert "normalize=0" in graph
    assert "-vf" not in command, "the video filter must move into the complex graph"


def test_mic_only_leaves_the_monitor_source_out(monkeypatch, tmp_path):
    monkeypatch.setattr(linux, "default_mic_source", lambda: "alsa_input.mic")
    inputs = LinuxX11Backend(tmp_path / "o.mp4", audio=False, mic=True).audio_inputs()
    assert len(inputs) == 1 and inputs[0].args[-1] == "alsa_input.mic"


def test_wayland_refuses_to_record_both_audio_sources(monkeypatch, tmp_path):
    monkeypatch.setattr(linux.shutil, "which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(RuntimeError, match="one audio source at a time"):
        LinuxWaylandBackend(tmp_path / "o.mp4", audio=True, mic=True).capture_command(tmp_path / "s.mkv")

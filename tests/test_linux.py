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

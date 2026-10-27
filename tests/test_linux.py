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


def test_wayland_uses_wf_recorder_and_stops_on_sigint(wayland, tmp_path):
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


TWO_OUTPUTS = [
    linux.Output("HDMI-A-1", (1920, 0, 1920, 1080)),
    linux.Output("DP-1", (0, 0, 1920, 1080), focused=True),
]


@pytest.fixture
def wayland(monkeypatch):
    """wf-recorder present, two monitors, no NVENC unless a test says so."""
    monkeypatch.setattr(linux.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(linux, "list_outputs", lambda: TWO_OUTPUTS)
    monkeypatch.setattr(linux, "ffmpeg_can", lambda *args: False)


def test_wayland_supports_region_but_not_window(wayland, tmp_path):
    cropped = LinuxWaylandBackend(tmp_path / "o.mp4", region=(10, 20, 800, 600), audio=False)
    command = cropped.capture_command(tmp_path / "s.mkv")
    assert command[command.index("-g") + 1] == "10,20 800x600"
    assert "-o" not in command
    with pytest.raises(RuntimeError, match="not supported on Wayland"):
        LinuxWaylandBackend(tmp_path / "o.mp4", window="Firefox").capture_command(tmp_path / "s.mkv")


def test_wayland_display_names_the_output(wayland, tmp_path):
    command = LinuxWaylandBackend(tmp_path / "o.mp4", display=0).capture_command(tmp_path / "s.mkv")
    assert command[command.index("-o") + 1] == "HDMI-A-1"
    with pytest.raises(RuntimeError, match="No display 2"):
        LinuxWaylandBackend(tmp_path / "o.mp4", display=2).capture_command(tmp_path / "s.mkv")


def test_wayland_always_tells_wf_recorder_which_output(wayland, tmp_path):
    """Found on Arch: left to choose between two monitors, wf-recorder asks on
    stdin - our pipe - and the segment came out empty."""
    backend = LinuxWaylandBackend(tmp_path / "o.mp4")
    command = backend.capture_command(tmp_path / "s.mkv")
    assert command[command.index("-o") + 1] == "DP-1", "the focused monitor"
    assert "DP-1" in backend.video_warning and "--display" in backend.video_warning


def test_wayland_one_monitor_needs_no_warning(wayland, monkeypatch, tmp_path):
    monkeypatch.setattr(linux, "list_outputs", lambda: TWO_OUTPUTS[:1])
    backend = LinuxWaylandBackend(tmp_path / "o.mp4")
    assert "HDMI-A-1" in backend.capture_command(tmp_path / "s.mkv")
    assert getattr(backend, "video_warning", None) is None


def test_wayland_captures_every_refresh_at_the_chosen_rate(wayland, tmp_path):
    """Damage-only capture left SIGINT unanswered on a still screen for 17s
    (measured), and made the video shorter than the audio it is lined up with."""
    command = LinuxWaylandBackend(tmp_path / "o.mp4", fps=45).capture_command(tmp_path / "s.mkv")
    assert "-D" in command
    assert command[command.index("-r") + 1] == "45"
    assert command[-2:] == ["-f", str(tmp_path / "s.mkv")]


def test_wayland_hands_nvenc_the_frames_unconverted(wayland, monkeypatch, tmp_path):
    from yefees_recorder.capture import NVENC_CQ

    monkeypatch.setattr(linux, "ffmpeg_can", lambda *args: True)
    command = LinuxWaylandBackend(tmp_path / "o.mp4", quality="high").capture_command(tmp_path / "s.mkv")
    assert command[command.index("-c") + 1] == "h264_nvenc"
    # yuv420p here moves the colour conversion onto the CPU: 46% against 19.5%.
    assert command[command.index("-x") + 1] == "bgr0"
    assert f"cq={NVENC_CQ['high']}" in command


def test_wayland_without_nvenc_uses_x264_at_the_quality_preset(wayland, tmp_path):
    command = LinuxWaylandBackend(tmp_path / "o.mp4", quality="low").capture_command(tmp_path / "s.mkv")
    assert command[command.index("-c") + 1] == "libx264"
    assert "preset=veryfast" in command and "crf=32" in command


def test_wayland_keeps_wf_recorder_chatter_off_the_terminal(tmp_path):
    backend = LinuxWaylandBackend(tmp_path / "o.mp4")
    assert backend.capture_log(tmp_path / "seg0.mkv") == tmp_path / "seg0.log"


HYPRCTL = """[{"id": 1, "name": "DP-1", "x": 0, "y": 0, "width": 1920, "height": 1080, "focused": true},
{"id": 0, "name": "HDMI-A-1", "x": 1920, "y": 0, "width": 1920, "height": 1080, "focused": false}]"""

WF_RECORDER_L = """1. Name: HDMI-A-1 Description: Samsung Electric Company SMBX2350 0x30303233 (HDMI-A-1)
2. Name: DP-1 Description: AOC 2460G5 0x00033235 (DP-1)
"""


def test_outputs_come_from_hyprland_in_its_own_order(monkeypatch):
    monkeypatch.setattr(linux, "_read", lambda cmd: HYPRCTL if cmd[0] == "hyprctl" else "")
    assert linux.list_outputs() == [
        linux.Output("HDMI-A-1", (1920, 0, 1920, 1080), False),
        linux.Output("DP-1", (0, 0, 1920, 1080), True),
    ]


def test_outputs_fall_back_to_wf_recorder_names(monkeypatch):
    replies = {"hyprctl": "HYPRLAND_INSTANCE_SIGNATURE not set!", "wf-recorder": WF_RECORDER_L}
    monkeypatch.setattr(linux, "_read", lambda cmd: replies[cmd[0]])
    assert [o.name for o in linux.list_outputs()] == ["HDMI-A-1", "DP-1"]


def test_wayland_sources_list_outputs_not_xwayland_monitors(monkeypatch):
    """XWayland's xrandr puts HDMI-A-1 at x=0 where Hyprland has DP-1."""
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setattr(linux, "list_outputs", lambda: TWO_OUTPUTS)
    monkeypatch.setattr(linux, "list_monitors", lambda: pytest.fail("xrandr asked on Wayland"))
    monkeypatch.setattr(linux, "_read", lambda cmd: "")
    displays = [s for s in linux.list_sources() if s.kind == "display"]
    assert [d.id for d in displays] == ["0", "1"]
    assert "HDMI-A-1" in displays[0].name and "(1920,0)" in displays[0].name


SOURCES = [
    "alsa_output.usb-Razer_BlackShark-00.analog-stereo.monitor",
    "alsa_input.usb-Razer_BlackShark-00.mono-fallback",
    "alsa_input.pci-0000_00_1f.3.analog-stereo",
]


def test_audio_sources_match_by_piece_of_name_and_kind():
    assert linux.find_source("Razer", SOURCES, monitor=True) == SOURCES[0]
    assert linux.find_source("Razer", SOURCES, monitor=False) == SOURCES[1]
    assert linux.find_source(SOURCES[2], SOURCES, monitor=False) == SOURCES[2]
    with pytest.raises(RuntimeError, match="No microphone source matching 'Yeti'"):
        linux.find_source("Yeti", SOURCES, monitor=False)
    assert linux.find_source("anything", [], monitor=True) == "anything", "no pactl to ask"


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


def audio_flag(command):
    flags = [arg for arg in command if arg.startswith("--audio")]
    return flags[0].split("=", 1)[1] if flags else None


@pytest.fixture
def pactl(monkeypatch):
    """Records pactl calls; load-module hands out increasing module numbers."""
    calls, modules = [], iter(range(100, 200))
    def run(arguments):
        calls.append(arguments)
        return str(next(modules)) if arguments[0] == "load-module" else ""
    monkeypatch.setattr(linux, "_pactl", run)
    monkeypatch.setattr(linux, "_read", lambda cmd: calls.append(cmd[1:]) or "")
    monkeypatch.setattr(linux, "_loopback_input", lambda module: f"input-of-{module}")
    monkeypatch.setattr(linux, "list_audio_sources", lambda: SOURCES)
    monkeypatch.setattr(linux, "default_mic_source", lambda: SOURCES[1])
    return calls


def test_wayland_one_source_goes_to_wf_recorder_as_it_is(wayland, pactl, tmp_path):
    """wf-recorder keeps its audio in time by its own clock - the only way it
    measured in sync - so a source it can take directly is given to it."""
    backend = LinuxWaylandBackend(tmp_path / "o.mp4")
    backend.setup()
    assert audio_flag(backend.capture_command(tmp_path / "s.mkv")) == "sink.monitor"
    assert pactl == [], "nothing loaded"
    assert backend.make_audio_recorder() is None, "no side recorder to line up"


def test_wayland_mixes_system_audio_and_mic_for_wf_recorder(wayland, pactl, tmp_path):
    backend = LinuxWaylandBackend(tmp_path / "o.mp4", mic=True, mic_gain=4.0)
    backend.setup()
    assert audio_flag(backend.capture_command(tmp_path / "s.mkv")) == "yefees_mix.monitor"
    loads = [call for call in pactl if call[0] == "load-module"]
    assert loads[0][1] == "module-null-sink"
    assert [call[2] for call in loads[1:]] == ["source=sink.monitor", f"source={SOURCES[1]}"]
    # A loopback that outlives its sink must not move on to the speakers.
    assert all("sink_dont_move=true" in call for call in loads[1:])
    # Measured: WirePlumber restored a saved volume over the gain once, and
    # would give a gain-1 source whatever the last recording set.
    assert all("sink_input_properties=state.restore-props=false" in call for call in loads[1:])
    # Linear, so "4.000000" and never "4" - pactl reads a bare integer as raw volume.
    assert ["set-sink-input-volume", "input-of-102", "4.000000"] in pactl
    assert not any(call[0] == "set-sink-input-volume" and call[1] == "input-of-101" for call in pactl)

    pactl.clear()
    backend.teardown()
    assert pactl == [["unload-module", "102"], ["unload-module", "101"], ["unload-module", "100"]]


def test_wayland_gain_alone_also_needs_the_mix(wayland, pactl, tmp_path):
    backend = LinuxWaylandBackend(tmp_path / "o.mp4", audio_gain=0.5)
    backend.setup()
    assert audio_flag(backend.capture_command(tmp_path / "s.mkv")) == "yefees_mix.monitor"
    assert ["set-sink-input-volume", "input-of-101", "0.500000"] in pactl


def test_wayland_without_audio_records_none(wayland, pactl, tmp_path):
    backend = LinuxWaylandBackend(tmp_path / "o.mp4", audio=False)
    backend.setup()
    assert audio_flag(backend.capture_command(tmp_path / "s.mkv")) is None


def test_wayland_unknown_mic_records_without_it(wayland, pactl, tmp_path):
    backend = LinuxWaylandBackend(tmp_path / "o.mp4", mic=True, mic_device="Yeti")
    backend.setup()
    assert audio_flag(backend.capture_command(tmp_path / "s.mkv")) == "sink.monitor"
    assert "Yeti" in backend.mic_error


def test_leftover_mix_is_unloaded_loopbacks_first(monkeypatch):
    """Unloaded after its sink, a loopback moves to the speakers and plays the mic."""
    listing = ("7\tmodule-null-sink\tsink_name=yefees_mix\n"
               "9\tmodule-loopback\tsource=mic sink=yefees_mix\n"
               "8\tmodule-loopback\tsource=x sink=yefees_mix\n"
               "5\tmodule-null-sink\tsink_name=other\n")
    unloaded = []
    monkeypatch.setattr(linux, "_read", lambda cmd: listing if cmd[1] == "list" else unloaded.append(cmd[2]))
    linux._unload_leftovers("yefees_mix")
    assert unloaded == ["9", "8", "7"]


SINK_INPUTS = """Sink Input #992
	Owner Module: n/a
Sink Input #1358
	Owner Module: 536870918
"""


def test_finds_the_stream_a_loopback_plays_through(monkeypatch):
    monkeypatch.setattr(linux, "_read", lambda cmd: SINK_INPUTS)
    assert linux._loopback_input("536870918") == "1358"
    assert linux._loopback_input("1") is None

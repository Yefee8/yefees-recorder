"""Linux backends.

X11 is straightforward: ffmpeg's x11grab reads the display and PulseAudio (or
PipeWire's Pulse compatibility layer) provides system audio as a second input,
so no side recorder or post-mux is needed.

Wayland cannot be captured by ffmpeg at all. Screen access goes through
xdg-desktop-portal over D-Bus and arrives as a PipeWire stream, and the
`pipewiregrab` patches that would have taught ffmpeg to read it were never
merged upstream. So the Wayland backend delegates to wf-recorder.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
from pathlib import Path

from .capture import AudioInput, CaptureBackend, Source

# `xrandr --listmonitors` prints e.g. " 1: +HDMI-1 1920/521x1080/293+1920+0  HDMI-1"
MONITOR_LINE = re.compile(r"(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)")

WAYLAND_HELP = (
    "Recording on Wayland needs wf-recorder, which is not installed.\n"
    "ffmpeg cannot capture Wayland itself - the screen is only reachable through "
    "xdg-desktop-portal/PipeWire.\n"
    "  Arch:   sudo pacman -S wf-recorder\n"
    "  Fedora: sudo dnf install wf-recorder\n"
    "  Debian: sudo apt install wf-recorder\n"
    "wf-recorder supports wlroots compositors (Sway, Hyprland, river). On GNOME or "
    "KDE, use your desktop's own screen recorder for now, or run an X11 session."
)


def _pactl(arguments: list[str]) -> str:
    """Run pactl and return its output, raising if it failed."""
    result = subprocess.run(["pactl", *arguments], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError(f"pactl {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _read(command: list[str]) -> str:
    """stdout of `command`, or "" if the tool is missing or fails."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode == 0 else ""


def list_monitors() -> list[tuple[int, int, int, int]]:
    """Each monitor as (x, y, width, height), from xrandr."""
    monitors = []
    for line in _read(["xrandr", "--listmonitors"]).splitlines():
        found = MONITOR_LINE.search(line)
        if found:
            width, height, x, y = (int(g) for g in found.groups())
            monitors.append((x, y, width, height))
    return monitors


def list_windows() -> list[tuple[str, str]]:
    """(window id, title) pairs from wmctrl, which reports both in one go."""
    windows = []
    for line in _read(["wmctrl", "-l"]).splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4:
            windows.append((parts[0], parts[3]))
    return windows


def resolve_window_id(title: str) -> str:
    matches = [(wid, name) for wid, name in list_windows() if title.lower() in name.lower()]
    if not matches:
        if not shutil.which("wmctrl"):
            raise RuntimeError("Selecting a window needs wmctrl (sudo apt install wmctrl).")
        raise RuntimeError(f"No window matching {title!r}. Run `yefees-recorder sources`.")
    return matches[0][0]


def list_audio_sources() -> list[str]:
    names = []
    for line in _read(["pactl", "list", "short", "sources"]).splitlines():
        fields = line.split("	")
        if len(fields) > 1:
            names.append(fields[1])
    return names


def default_mic_source() -> str:
    """The default PulseAudio capture source, i.e. the microphone."""
    result = _read(["pactl", "get-default-source"]).strip()
    return result or "default"


SINK_INPUT = re.compile(r"Sink Input #(\d+)")
APP_NAME = re.compile(r'application\.name = "([^"]*)"')

# The sink recordings are routed through while one application is captured.
CAPTURE_SINK = "yefees_capture"


def list_applications() -> list[tuple[str, str]]:
    """(sink-input id, application name) for everything currently playing.

    Only applications actually producing audio appear here - a silent or
    closed app has no sink input.
    """
    found, current = [], None
    for line in _read(["pactl", "list", "sink-inputs"]).splitlines():
        header = SINK_INPUT.search(line)
        if header:
            current = header.group(1)
            continue
        name = APP_NAME.search(line)
        if name and current is not None:
            found.append((current, name.group(1)))
            current = None
    return found


def list_sources() -> list[Source]:
    sources = [
        Source("display", str(index), f"Display {index} - {w}x{h} at ({x},{y})")
        for index, (x, y, w, h) in enumerate(list_monitors())
    ]
    sources += [Source("window", title, title) for _, title in list_windows()]
    for name in list_audio_sources():
        # A sink's .monitor is what carries system audio; everything else is an
        # actual capture device, i.e. a microphone.
        if name.endswith(".monitor"):
            sources.append(Source("audio", name, "system audio"))
        else:
            sources.append(Source("mic", name, "microphone"))
    sources += [Source("app", name, "application audio") for _, name in list_applications()]
    return sources


def default_monitor_source() -> str:
    """The PulseAudio/PipeWire monitor source for the current output device.

    Recording the sink's `.monitor` is what captures system audio; the plain
    default source is the microphone.
    """
    try:
        result = subprocess.run(
            ["pactl", "get-default-sink"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip() + ".monitor"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "default"


class LinuxX11Backend(CaptureBackend):
    def _display_region(self) -> tuple[int, int, int, int] | None:
        if self.display is None:
            return None
        monitors = list_monitors()
        if self.display >= len(monitors):
            raise RuntimeError(
                f"No display {self.display}; xrandr reports {len(monitors)}. "
                "Run `yefees-recorder sources`."
            )
        return monitors[self.display]

    def video_input_args(self) -> list[str]:
        args = ["-f", "x11grab", "-framerate", str(self.fps)]
        display = os.environ.get("DISPLAY", ":0.0")
        if self.window:
            # x11grab sizes itself from the window, so no -video_size here.
            return args + ["-window_id", resolve_window_id(self.window), "-i", display]
        region = self.region or self._display_region()
        if region:
            x, y, width, height = region
            args += ["-video_size", f"{width}x{height}"]
            display = f"{display}+{x},{y}"
        # Without -video_size x11grab works the screen size out itself.
        return args + ["-i", display]

    def setup(self) -> None:
        self._modules = []
        if not self.app_audio:
            return
        if not shutil.which("pactl"):
            raise RuntimeError("Recording one application needs pactl (PulseAudio or PipeWire).")

        matches = [i for i, name in list_applications() if self.app_audio.lower() in name.lower()]
        if not matches:
            playing = ", ".join(name for _, name in list_applications()) or "nothing is playing"
            raise RuntimeError(
                f"No application matching {self.app_audio!r}. Currently playing: {playing}"
            )

        sink = _read(["pactl", "get-default-sink"]).strip()
        try:
            # A null sink to capture from, plus a loopback of it to the real
            # output - without that second module the user stops hearing the app
            # they are recording.
            self._modules.append(_pactl(
                ["load-module", "module-null-sink", f"sink_name={CAPTURE_SINK}",
                 f"sink_properties=device.description={CAPTURE_SINK}"]))
            self._modules.append(_pactl(
                ["load-module", "module-loopback",
                 f"source={CAPTURE_SINK}.monitor", f"sink={sink}", "latency_msec=50"]))
            for sink_input in matches:
                _pactl(["move-sink-input", sink_input, CAPTURE_SINK])
        except Exception:
            self.teardown()
            raise

    def teardown(self) -> None:
        """Unload our modules, which moves the application's audio back."""
        for module in reversed(getattr(self, "_modules", [])):
            _read(["pactl", "unload-module", module])
        self._modules = []

    def audio_inputs(self) -> list[AudioInput]:
        inputs = []
        if self.want_audio:
            if self.app_audio:
                source = f"{CAPTURE_SINK}.monitor"
            else:
                source = self.audio_device or default_monitor_source()
            inputs.append(AudioInput(["-f", "pulse", "-i", source], self.audio_gain))
        if self.want_mic:
            inputs.append(AudioInput(
                ["-f", "pulse", "-i", self.mic_device or default_mic_source()], self.mic_gain))
        return inputs


class LinuxWaylandBackend(CaptureBackend):
    # wf-recorder finalises its file on SIGINT; it has no stdin protocol.
    stop_signal = signal.SIGINT

    def video_input_args(self) -> list[str]:
        raise NotImplementedError("Wayland does not go through ffmpeg")  # capture_command overridden

    def capture_command(self, output: Path) -> list[str]:
        if not shutil.which("wf-recorder"):
            raise RuntimeError(WAYLAND_HELP)
        if self.window is not None or self.display is not None:
            raise RuntimeError(
                "Selecting a window or display is not supported on Wayland - the "
                "compositor decides what a recorder may see. Use --region, or run "
                "wf-recorder directly with its own -o/-g options."
            )
        if self.app_audio:
            raise RuntimeError(
                "Recording one application is only wired up for X11 so far; on Wayland, "
                "route the app with pactl yourself and pass --audio-device."
            )
        if self.want_audio and self.want_mic:
            raise RuntimeError(
                "wf-recorder records one audio source at a time, so system audio and "
                "the microphone cannot both be captured on Wayland. Turn one off, or "
                "mix them into a virtual sink with pactl first."
            )
        command = ["wf-recorder", "-f", str(output)]
        if self.region:
            x, y, width, height = self.region
            command += ["-g", f"{x},{y} {width}x{height}"]
        if self.want_audio:
            command.append(f"--audio={self.audio_device or default_monitor_source()}")
        elif self.want_mic:
            command.append(f"--audio={self.mic_device or default_mic_source()}")
        return command

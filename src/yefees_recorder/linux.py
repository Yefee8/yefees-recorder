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

from .capture import CaptureBackend, Source

# `xrandr --listmonitors` prints e.g. " 1: +HDMI-1 1920/521x1080/293+1920+0  HDMI-1"
MONITOR_LINE = re.compile(r"(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)")

WAYLAND_HELP = (
    "Recording on Wayland needs wf-recorder, which is not installed.\n"
    "ffmpeg cannot capture Wayland itself — the screen is only reachable through "
    "xdg-desktop-portal/PipeWire.\n"
    "  Arch:   sudo pacman -S wf-recorder\n"
    "  Fedora: sudo dnf install wf-recorder\n"
    "  Debian: sudo apt install wf-recorder\n"
    "wf-recorder supports wlroots compositors (Sway, Hyprland, river). On GNOME or "
    "KDE, use your desktop's own screen recorder for now, or run an X11 session."
)


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


def list_sources() -> list[Source]:
    sources = [
        Source("display", str(index), f"Display {index} — {w}x{h} at ({x},{y})")
        for index, (x, y, w, h) in enumerate(list_monitors())
    ]
    sources += [Source("window", title, title) for _, title in list_windows()]
    sources += [
        Source("audio", name, name + (" (system audio)" if name.endswith(".monitor") else ""))
        for name in list_audio_sources()
    ]
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

    def audio_input_args(self) -> list[str]:
        return ["-f", "pulse", "-i", self.audio_device or default_monitor_source()]


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
                "Selecting a window or display is not supported on Wayland — the "
                "compositor decides what a recorder may see. Use --region, or run "
                "wf-recorder directly with its own -o/-g options."
            )
        command = ["wf-recorder", "-f", str(output)]
        if self.region:
            x, y, width, height = self.region
            command += ["-g", f"{x},{y} {width}x{height}"]
        if self.want_audio:
            command.append(f"--audio={self.audio_device or default_monitor_source()}")
        return command

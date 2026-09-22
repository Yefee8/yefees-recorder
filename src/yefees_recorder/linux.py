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
import shutil
import signal
import subprocess
from pathlib import Path

from .capture import CaptureBackend

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
    def video_input_args(self) -> list[str]:
        # x11grab works out the screen size itself when -video_size is omitted.
        return ["-f", "x11grab", "-framerate", str(self.fps), "-i", os.environ.get("DISPLAY", ":0.0")]

    def audio_input_args(self) -> list[str]:
        return ["-f", "pulse", "-i", default_monitor_source()]


class LinuxWaylandBackend(CaptureBackend):
    # wf-recorder finalises its file on SIGINT; it has no stdin protocol.
    stop_signal = signal.SIGINT

    def video_input_args(self) -> list[str]:
        raise NotImplementedError("Wayland does not go through ffmpeg")  # capture_command overridden

    def capture_command(self, output: Path) -> list[str]:
        if not shutil.which("wf-recorder"):
            raise RuntimeError(WAYLAND_HELP)
        command = ["wf-recorder", "-f", str(output)]
        if self.want_audio:
            command.append(f"--audio={default_monitor_source()}")
        return command

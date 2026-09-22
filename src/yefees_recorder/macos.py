"""macOS backend.

Video comes from avfoundation's "Capture screen" device. System audio has no
native loopback on macOS, so it needs a virtual output device (BlackHole and
friends) that shows up as an *input* ffmpeg can read; without one, we record
video only rather than failing.

The trap worth knowing: without Screen Recording permission macOS does not
error, it hands back black frames. So permission is checked up front via
CoreGraphics instead of being discovered after a ruined recording.
"""

from __future__ import annotations

import ctypes
import re
import subprocess
from pathlib import Path

from .capture import CaptureBackend

# Virtual output devices that loop system audio back to an input.
LOOPBACK_DEVICE_HINTS = ("blackhole", "soundflower", "loopback audio", "ishowu", "multi-output")

DEVICE_LINE = re.compile(r"\[(\d+)\]\s+(.+?)\s*$")

PERMISSION_HELP = (
    "Screen Recording permission has not been granted, so macOS would record "
    "black frames instead of your screen.\n"
    "Grant it in System Settings > Privacy & Security > Screen Recording, tick the "
    "terminal app you are running this from, then run the command again.\n"
    "(macOS only applies the change to newly launched processes, so restart the "
    "terminal if it still fails.)"
)

BLACKHOLE_HELP = (
    "No loopback audio device found, so system audio cannot be captured — macOS "
    "has no built-in way to record its own output.\n"
    "Install one with `brew install blackhole-2ch`, then in Audio MIDI Setup create "
    "a Multi-Output Device containing both BlackHole and your speakers and select it "
    "as the system output (otherwise you record the audio but stop hearing it)."
)


def list_avfoundation_devices() -> tuple[dict[int, str], dict[int, str]]:
    """The (video, audio) devices ffmpeg can see, as {index: name}.

    `-list_devices` writes to stderr and exits non-zero by design, so the exit
    code is deliberately ignored.
    """
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True,
        text=True,
    )
    video: dict[int, str] = {}
    audio: dict[int, str] = {}
    section = None
    for line in result.stderr.splitlines():
        if "video devices" in line:
            section = video
            continue
        if "audio devices" in line:
            section = audio
            continue
        match = DEVICE_LINE.search(line)
        if match and section is not None:
            section[int(match.group(1))] = match.group(2)
    return video, audio


def find_screen_device(video_devices: dict[int, str]) -> int | None:
    for index, name in sorted(video_devices.items()):
        if name.lower().startswith("capture screen"):
            return index
    return None


def find_loopback_device(audio_devices: dict[int, str]) -> int | None:
    for index, name in sorted(audio_devices.items()):
        if any(hint in name.lower() for hint in LOOPBACK_DEVICE_HINTS):
            return index
    return None


def _coregraphics():
    return ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
    )


def screen_recording_permitted() -> bool | None:
    """True, False, or None when the answer cannot be determined."""
    try:
        preflight = _coregraphics().CGPreflightScreenCaptureAccess
        preflight.restype = ctypes.c_bool
        return bool(preflight())
    except (OSError, AttributeError):
        return None


def request_screen_recording() -> bool:
    """Ask macOS to show the Screen Recording permission prompt."""
    try:
        request = _coregraphics().CGRequestScreenCaptureAccess
        request.restype = ctypes.c_bool
        return bool(request())
    except (OSError, AttributeError):
        return False


class MacBackend(CaptureBackend):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._devices = None

    @property
    def devices(self) -> tuple[dict[int, str], dict[int, str]]:
        if self._devices is None:
            self._devices = list_avfoundation_devices()
        return self._devices

    def video_input_args(self) -> list[str]:
        if screen_recording_permitted() is False:
            request_screen_recording()  # surfaces the system prompt
            raise RuntimeError(PERMISSION_HELP)
        screen = find_screen_device(self.devices[0])
        if screen is None:
            raise RuntimeError(
                "ffmpeg found no 'Capture screen' device. This usually means Screen "
                "Recording permission is missing for this terminal."
            )
        return [
            "-f", "avfoundation",
            "-framerate", str(self.fps),
            "-capture_cursor", "1",
            "-i", f"{screen}:none",
        ]

    def audio_input_args(self) -> list[str]:
        # A second avfoundation input rather than "screen:audio" in one, which
        # is prone to drift between the two streams.
        loopback = find_loopback_device(self.devices[1])
        if loopback is None:
            self.audio_error = BLACKHOLE_HELP
            return []
        return ["-f", "avfoundation", "-i", f"none:{loopback}"]

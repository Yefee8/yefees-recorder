"""Capture engine.

ffmpeg does the capturing and encoding; each platform backend only supplies the
input arguments for its screen-grab device, and gets system audio either as a
second ffmpeg input (Linux) or from a side recorder that is muxed in afterwards
(Windows, where dshow has no loopback device).

Pause works by ending the current capture process and starting a new one on
resume; segments are muxed with their audio and concatenated on stop.

A backend that cannot use ffmpeg at all (Wayland) overrides `capture_command`
with its own recorder and keeps the rest of the lifecycle.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import NamedTuple

FFMPEG_BASE = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]

# gdigrab/x11grab can hand back odd dimensions, which yuv420p cannot encode.
EVEN_DIMS = "pad=ceil(iw/2)*2:ceil(ih/2)*2"

# x264 speed/size trade-offs. Screen capture is realtime, so even "high" stays
# well away from the slow presets — dropping frames costs more than bitrate does.
QUALITY_PRESETS = {
    "low": ("veryfast", 32),
    "balanced": ("veryfast", 26),
    "high": ("medium", 20),
}

REGION = re.compile(r"^\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(\d+)\s*x\s*(\d+)\s*$")


class Source(NamedTuple):
    """Something that can be recorded, as offered to the user."""

    kind: str   # "display", "window" or "audio"
    id: str     # what to pass back on the command line
    name: str   # human-readable label


def parse_region(text: str) -> tuple[int, int, int, int]:
    """`x,y,WxH` -> (x, y, width, height)."""
    match = REGION.match(text)
    if not match:
        raise ValueError(f"expected a region like 0,0,1920x1080 — got {text!r}")
    x, y, width, height = (int(g) for g in match.groups())
    if width <= 0 or height <= 0:
        raise ValueError(f"region must have a positive size — got {text!r}")
    return x, y, width, height


# Don't let Ctrl+C in our terminal reach ffmpeg; we stop it deliberately with "q".
_NEW_GROUP = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0


def _run(args: list[str]) -> None:
    proc = subprocess.run(FFMPEG_BASE + args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-400:]}")


class CaptureBackend(ABC):
    """Base recorder. Subclasses describe *what* to capture, this drives it."""

    def __init__(
        self,
        output: Path,
        fps: int = 30,
        audio: bool = True,
        quality: str = "balanced",
        audio_offset: float = 0.0,
        display: int | None = None,
        window: str | None = None,
        region: tuple[int, int, int, int] | None = None,
        audio_device: str | None = None,
        mic: bool = False,
        mic_device: str | None = None,
    ) -> None:
        self.output = Path(output)
        self.fps = fps
        self.want_audio = audio
        if quality not in QUALITY_PRESETS:
            raise ValueError(f"unknown quality {quality!r}; pick one of {sorted(QUALITY_PRESETS)}")
        self.preset, self.crf = QUALITY_PRESETS[quality]
        # Which screen, window or area to record, and which audio device to use.
        # A backend resolves `display` into a region if that is how its capture
        # device addresses monitors.
        self.display = display
        self.window = window
        self.region = region
        self.audio_device = audio_device
        # System audio and the microphone are independent: either, both or
        # neither. When both are on they are mixed down to a single track.
        self.want_mic = mic
        self.mic_device = mic_device
        self.mic_error: str | None = None
        # ponytail: fixed A/V nudge in seconds; per-machine calibration knob.
        # Raise it if audio runs early, lower it if audio runs late.
        self.audio_offset = audio_offset
        self._tmpdir = tempfile.TemporaryDirectory(prefix="yefees-")
        self.workdir = Path(self._tmpdir.name)
        self._segments: list[tuple[Path, Path | None]] = []
        self._proc: subprocess.Popen | None = None
        self._audio = None

    # How to ask the capture process to finish. None means ffmpeg: write "q" to
    # its stdin. Anything else is a signal number to send instead.
    stop_signal = None

    @abstractmethod
    def video_input_args(self) -> list[str]:
        """ffmpeg args opening this platform's screen capture device."""

    def audio_inputs(self) -> list[list[str]]:
        """ffmpeg args for each audio input this platform can capture directly.

        One list per input, so the caller knows how many there are and can mix
        them. Empty means either that there is no audio, or that it comes from
        a side recorder instead (Windows system audio).
        """
        return []

    def video_filters(self) -> list[str]:
        """Filters applied to the captured video, innermost first."""
        return [EVEN_DIMS]

    def capture_command(self, output: Path) -> list[str]:
        """The full command recording one segment to `output`."""
        inputs = self.audio_inputs()
        args = FFMPEG_BASE + self.video_input_args()
        for one in inputs:
            args += one

        if len(inputs) > 1:
            # -vf and -filter_complex cannot both be used, so when several audio
            # inputs have to be mixed the video filter moves into the complex
            # graph as well.
            # normalize=0 keeps each source at its own level; amix otherwise
            # scales every input by 1/n, which measurably quietens system audio
            # the moment a microphone is added.
            taps = "".join(f"[{n}:a]" for n in range(1, len(inputs) + 1))
            graph = (
                f"[0:v]{','.join(self.video_filters())}[vout];"
                f"{taps}amix=inputs={len(inputs)}:duration=longest:normalize=0[aout]"
            )
            args += ["-filter_complex", graph, "-map", "[vout]", "-map", "[aout]"]
        else:
            args += ["-vf", ",".join(self.video_filters())]

        args += ["-c:v", "libx264", "-preset", self.preset, "-crf", str(self.crf),
                 "-pix_fmt", "yuv420p"]
        if inputs:
            args += ["-c:a", "aac"]
        return args + [str(output)]

    def make_audio_recorder(self):
        """A side recorder for audio ffmpeg cannot capture, or None.

        Used on Windows, where dshow exposes no loopback device. Its output is
        mixed in afterwards, alongside anything `audio_inputs` captured.
        """
        return None

    @property
    def recording(self) -> bool:
        return self._proc is not None

    def start(self) -> None:
        if self.recording:
            raise RuntimeError("already recording")
        self._start_segment()

    def pause(self) -> None:
        if not self.recording:
            raise RuntimeError("not recording")
        self._end_segment()

    def resume(self) -> None:
        if self.recording:
            raise RuntimeError("not paused")
        self._start_segment()

    def stop(self) -> Path:
        self._end_segment()
        parts = [
            self._mux(i, video, audio)
            for i, (video, audio) in enumerate(self._segments)
            if video.exists() and video.stat().st_size > 0
        ]
        if not parts:
            raise RuntimeError("ffmpeg captured nothing — try `yefees-recorder doctor`")
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self._concat(parts)
        self._tmpdir.cleanup()
        return self.output

    def _start_segment(self) -> None:
        index = len(self._segments)
        video = self.workdir / f"seg{index}.mkv"
        audio = None
        if self.want_audio or self.want_mic:
            recorder = self.make_audio_recorder()
            if recorder is not None:
                audio = self.workdir / f"seg{index}.wav"
                recorder.start(audio)
                self._audio = recorder
        self._proc = subprocess.Popen(
            self.capture_command(video),
            stdin=subprocess.PIPE,
            creationflags=_NEW_GROUP,
        )
        self._segments.append((video, audio))

    def _end_segment(self) -> None:
        if self._proc is None:
            if self._audio is not None:
                self._audio.stop()
                self._audio = None
            return
        try:
            if self.stop_signal is None:
                self._proc.stdin.write(b"q")  # ffmpeg finalizes the file on "q"
                self._proc.stdin.flush()
            else:
                self._proc.send_signal(self.stop_signal)
            # Stop audio between the signal and the wait, so the two streams end
            # within a few ms of each other instead of a device-teardown apart.
            if self._audio is not None:
                self._audio.stop()
                self._audio = None
            self._proc.wait(timeout=15)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            self._proc.kill()
            self._proc.wait()
        finally:
            self._proc = None
            if self._audio is not None:
                self._audio.stop()
                self._audio = None

    def _mux(self, index: int, video: Path, audio: Path | None) -> Path:
        if audio is None or not audio.exists() or audio.stat().st_size == 0:
            return video
        merged = self.workdir / f"mux{index}.mkv"
        offset = ["-itsoffset", str(self.audio_offset)] if self.audio_offset else []
        args = ["-i", str(video), *offset, "-i", str(audio)]
        if self.audio_inputs():
            # The segment already carries a track (a microphone, say), so the
            # side recording has to be mixed with it rather than replace it.
            args += [
                "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=longest:normalize=0[aout]",
                "-map", "0:v", "-map", "[aout]",
            ]
        args += ["-c:v", "copy", "-c:a", "aac", str(merged)]
        _run(args)
        return merged

    def _concat(self, parts: list[Path]) -> None:
        listing = self.workdir / "segments.txt"
        listing.write_text("\n".join(f"file '{p.as_posix()}'" for p in parts), encoding="utf-8")
        _run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(self.output)])


def _platform_module():
    """The module implementing this OS, or None when there isn't one."""
    import importlib
    import platform

    modules = {"Windows": ".windows", "Darwin": ".macos", "Linux": ".linux"}
    name = modules.get(platform.system())
    return importlib.import_module(name, __package__) if name else None


def list_sources() -> list[Source]:
    """Every display, window and audio device this OS can offer."""
    import platform

    module = _platform_module()
    if module is None or not hasattr(module, "list_sources"):
        raise NotImplementedError(f"Cannot enumerate sources on {platform.system()}.")
    return module.list_sources()


def get_backend(output: Path, **kwargs) -> CaptureBackend:
    """The recorder for the current OS."""
    import platform

    import os

    system = platform.system()
    if system == "Windows":
        from .windows import WindowsBackend

        return WindowsBackend(output, **kwargs)
    if system == "Linux":
        from .linux import LinuxWaylandBackend, LinuxX11Backend

        wayland = os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
        backend = LinuxWaylandBackend if wayland else LinuxX11Backend
        return backend(output, **kwargs)
    if system == "Darwin":
        from .macos import MacBackend

        return MacBackend(output, **kwargs)
    raise NotImplementedError(f"No backend for {system}.")

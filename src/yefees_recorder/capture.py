"""Capture engine.

ffmpeg does the capturing and encoding; each platform backend only supplies the
input arguments for its screen-grab device, and gets audio either as further
ffmpeg inputs (Linux) or from a side recorder that is muxed in afterwards
(Windows and macOS, where sharing the capture process costs frames or samples).

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

# -stats_period: ffmpeg only looks at stdin for "q" once a stats period, which is
# half a second by default - so capture ran on 0.28-0.40s past the request, the
# side audio did not, and `audio_lead` read the difference as a late start.
# Measured with wall-clock frame stamps: 0.01-0.10s at 0.02.
# ponytail: "q" is still read at most every 100 ms; a console signal stopped
# within 35 ms but exits 255 and needs a shared console on Windows.
FFMPEG_BASE = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stats_period", "0.02", "-y"]

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

# How long past its duration to let a self-timing recorder finish before giving
# up on it. It stops later than a clock started at launch would, because it
# counts from its first captured frame - which is the entire point of it.
STARTUP_GRACE = 10.0


class AudioInput(NamedTuple):
    """One ffmpeg audio input, with the gain to apply before mixing.

    Microphones typically sit far below system audio, so without a gain the
    voice is inaudible under the game or video being recorded.
    """

    args: list[str]
    gain: float = 1.0


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


def segment_seconds(path: Path) -> float:
    """Seconds of media in a finished file, or 0 when it cannot be read."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True,
        text=True,
    )
    try:
        return float(probe.stdout.strip())
    except ValueError:
        return 0.0


def _run(args: list[str]) -> None:
    proc = subprocess.run(FFMPEG_BASE + args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-400:]}")


class MixedRecorder:
    """Several audio devices, each recorded to a wav of its own, mixed on stop.

    Nothing is mixed live: measured on macOS, amix between the devices and the
    file turned beeps a second apart into 50 ms fragments, while a file per
    device kept every one of them. Mixing the finished files costs a fraction of
    a second.

    `devices` is (label, recorder, gain) - the label naming the `<label>_error`
    a device that will not open is reported on, so the other one still records.
    """

    def __init__(self, devices: list[tuple[str, object, float]]) -> None:
        self.devices = devices
        self.errors: dict[str, str] = {}
        self._target: Path | None = None
        self._parts: list[tuple[Path, float]] = []

    def start(self, path: Path) -> None:
        self._target = Path(path)
        for number, (label, recorder, gain) in enumerate(self.devices):
            part = self._target.with_name(f"{self._target.stem}-{number}.wav")
            try:
                recorder.start(part)
            except Exception as exc:  # device gone, in use, or refused
                self.errors[label] = f"Could not open the {label} device, recording without it: {exc}"
                continue
            self._parts.append((part, gain))

    def stop(self) -> None:
        for _, recorder, _ in self.devices:
            recorder.stop()
        usable = [(p, gain) for p, gain in self._parts if p.exists() and p.stat().st_size > 0]
        if not usable or self._target is None:
            return  # no wav at all reads as "video only" when muxed
        if len(usable) == 1 and usable[0][1] == 1.0:
            usable[0][0].replace(self._target)  # renaming beats re-encoding
            return
        args = list(FFMPEG_BASE)
        for part, _ in usable:
            args += ["-i", str(part)]
        levels = "".join(f"[{n}:a]volume={gain}[a{n}];" for n, (_, gain) in enumerate(usable))
        taps = "".join(f"[a{n}]" for n in range(len(usable)))
        # normalize=0 keeps each source at its own level; amix otherwise scales
        # every input by 1/n, which measurably quietens system audio the moment
        # a microphone is added.
        graph = f"{levels}{taps}amix=inputs={len(usable)}:duration=longest:normalize=0[out]"
        # Not checked: a mix that fails leaves no wav, which is "video only"
        # rather than a recording lost to an exception.
        subprocess.run(args + ["-filter_complex", graph, "-map", "[out]", "-c:a", "pcm_s16le",
                               str(self._target)], capture_output=True)


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
        audio_gain: float = 1.0,
        mic_gain: float = 1.0,
        app_audio: str | None = None,
        duration: float = 0.0,
    ) -> None:
        self.output = Path(output)
        self.fps = fps
        self.want_audio = audio
        if quality not in QUALITY_PRESETS:
            raise ValueError(f"unknown quality {quality!r}; pick one of {sorted(QUALITY_PRESETS)}")
        self.quality = quality
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
        self.audio_gain = audio_gain
        self.mic_gain = mic_gain
        self.app_audio = app_audio
        # Seconds to capture, 0 meaning "until stopped". Only a backend that
        # sets `limits_duration` acts on it; for the rest the caller times the
        # recording from the outside, as it always has.
        self.duration = duration
        # ponytail: fixed A/V nudge in seconds; per-machine calibration knob.
        # Raise it if audio runs early, lower it if audio runs late.
        self.audio_offset = audio_offset
        self._tmpdir = tempfile.TemporaryDirectory(prefix="yefees-")
        self.workdir = Path(self._tmpdir.name)
        self._segments: list[tuple[Path, Path | None]] = []
        self._proc: subprocess.Popen | None = None
        self._audio = None
        self._finished: Path | None = None

    # How to ask the capture process to finish. None means ffmpeg: write "q" to
    # its stdin. Anything else is a signal number to send instead.
    stop_signal = None

    # Whether the recorder enforces `duration` itself. A device that is slow to
    # wake up makes a duration timed from launch come out short, so a backend
    # that can count from its first captured frame says so here and the caller
    # waits for it to finish instead of stopping it on a clock.
    limits_duration = False

    @abstractmethod
    def video_input_args(self) -> list[str]:
        """ffmpeg args opening this platform's screen capture device."""

    def audio_inputs(self) -> list[AudioInput]:
        """ffmpeg args for each audio input this platform can capture directly.

        One entry per input, so the caller knows how many there are and can mix
        them at the right levels. Empty means either that there is no audio, or that it comes from
        a side recorder instead (Windows system audio).
        """
        return []

    def video_filters(self) -> list[str]:
        """Filters applied to the captured video, innermost first.

        May be empty, for frames that never leave the GPU and so cannot go
        through a software filter at all.
        """
        return [EVEN_DIMS]

    def video_codec_args(self) -> list[str]:
        """The encoder. A backend with a hardware encoder it can feed swaps it in."""
        return ["-c:v", "libx264", "-preset", self.preset, "-crf", str(self.crf),
                "-pix_fmt", "yuv420p"]

    def output_options(self) -> list[str]:
        """Extra ffmpeg options for the segment, just before its file name."""
        return []

    def capture_command(self, output: Path) -> list[str]:
        """The full command recording one segment to `output`."""
        inputs = self.audio_inputs()
        args = FFMPEG_BASE + self.video_input_args()
        for one in inputs:
            args += one.args
        video_chain = ",".join(self.video_filters()) or "null"

        if len(inputs) > 1:
            # -vf and -filter_complex cannot both be used, so when several audio
            # inputs have to be mixed the video filter moves into the complex
            # graph as well.
            chains, taps = [], []
            for number, one in enumerate(inputs, start=1):
                if one.gain != 1.0:
                    chains.append(f"[{number}:a]volume={one.gain}[g{number}]")
                    taps.append(f"[g{number}]")
                else:
                    taps.append(f"[{number}:a]")
            # normalize=0 keeps each source at its own level; amix otherwise
            # scales every input by 1/n, which measurably quietens system audio
            # the moment a microphone is added.
            graph = f"[0:v]{video_chain}[vout];"
            graph += "".join(chain + ";" for chain in chains)
            graph += "".join(taps) + f"amix=inputs={len(inputs)}:duration=longest:normalize=0[aout]"
            args += ["-filter_complex", graph, "-map", "[vout]", "-map", "[aout]"]
        else:
            args += ["-vf", video_chain]
            if inputs and inputs[0].gain != 1.0:
                args += ["-af", f"volume={inputs[0].gain}"]

        args += self.video_codec_args()
        if inputs:
            args += ["-c:a", "aac"]
        return args + self.output_options() + [str(output)]

    def audio_lead(self, video: Path, audio: Path) -> float:
        """Seconds by which a side recording starts before the video does.

        Negative when it starts after the video instead. Both are stopped within
        milliseconds of each other, so whatever length one has over the other it
        has at the front. Neither the size nor the sign is fixed: on macOS the
        screen opened 0.74s after the audio on one segment and 1.02s before it
        on the next; on Windows ddagrab and NVENC take 0.36s to the first frame
        where gdigrab took next to nothing. Left alone it puts the sound behind
        the picture, a segment further at every pause.
        """
        return segment_seconds(audio) - segment_seconds(video)

    def make_audio_recorder(self):
        """A side recorder for audio kept out of the capture process, or None.

        Its wav is muxed in afterwards, already mixed at each device's gain. A
        backend uses one when sharing a process with the screen costs frames or
        samples - which is the case on Windows and macOS.
        """
        return None

    @property
    def recording(self) -> bool:
        return self._proc is not None

    @property
    def capture_ended(self) -> bool:
        """Whether the capture process stopped on its own rather than paused."""
        return self._proc is not None and self._proc.poll() is not None

    def setup(self) -> None:
        """Prepare anything the capture needs, before the first segment.

        Whatever this changes on the system must be undone by `teardown`.
        """

    def teardown(self) -> None:
        """Undo `setup`. Always called, even when recording failed."""

    def start(self) -> None:
        if self.recording:
            raise RuntimeError("already recording")
        self.setup()
        try:
            self._start_segment()
        except Exception:
            self.teardown()
            raise

    def pause(self) -> None:
        if not self.recording:
            raise RuntimeError("not recording")
        self._end_segment()

    def resume(self) -> None:
        if self.recording:
            raise RuntimeError("not paused")
        self._start_segment()

    def stop(self) -> Path:
        """Finish the recording. Safe to call twice; the second call is a no-op.

        An abrupt exit and the normal end of a recording both land here, and
        they can race, so the finished path is remembered rather than redone.
        """
        if self._finished is not None:
            return self._finished
        self._end_segment()
        self.teardown()
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
        self._finished = self.output
        return self.output

    def _start_segment(self) -> None:
        index = len(self._segments)
        video = self.workdir / f"seg{index}.mkv"
        # Built before any audio starts: it can take a while (probing the GPU,
        # measuring earlier segments), and a side recorder would count all of it
        # as sound that came before the first frame.
        command = self.capture_command(video)
        audio = None
        if self.want_audio or self.want_mic:
            recorder = self.make_audio_recorder()
            if recorder is not None:
                audio = self.workdir / f"seg{index}.wav"
                recorder.start(audio)
                self._audio = recorder
        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            creationflags=_NEW_GROUP,
        )
        self._segments.append((video, audio))

    def check_audio(self) -> None:
        """Put any device the side recorder could not open on `<label>_error`.

        Reported, not raised: the screen is still worth having, and `_mux`
        already treats a missing wav as "video only".
        """
        for label, failure in getattr(self._audio, "errors", {}).items():
            setattr(self, f"{label}_error", failure)

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
        # A wav older than the first frame has its head seeked past - seeking a
        # PCM file is exact. One that starts after the first frame is pushed
        # back by real silence instead of a timestamp shift, because the parts
        # are concatenated with -c copy afterwards and samples say what a gap in
        # the timestamps only implies.
        lead = self.audio_lead(video, audio)
        head = ["-ss", f"{lead:.3f}"] if lead > 0 else []
        offset = ["-itsoffset", str(self.audio_offset)] if self.audio_offset else []
        args = ["-i", str(video), *head, *offset, "-i", str(audio)]
        if lead < 0:
            args += ["-filter:a", f"adelay=delays={-lead * 1000:.0f}ms:all=1"]
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

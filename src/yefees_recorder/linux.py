"""Linux backends.

X11 is straightforward: ffmpeg's x11grab reads the display and PulseAudio (or
PipeWire's Pulse compatibility layer) provides system audio as a second input,
so no side recorder or post-mux is needed.

Wayland cannot be captured by ffmpeg at all. Screen access goes through
xdg-desktop-portal over D-Bus and arrives as a PipeWire stream, and the
`pipewiregrab` patches that would have taught ffmpeg to read it were never
merged upstream. So the Wayland backend delegates to wf-recorder, audio
included: it keeps the two in time by its own clock, which nothing outside it
can see.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path
from typing import NamedTuple

from .capture import NVENC_CQ, AudioInput, CaptureBackend, Source, ffmpeg_can

# `xrandr --listmonitors` prints e.g. " 1: +HDMI-1 1920/521x1080/293+1920+0  HDMI-1"
MONITOR_LINE = re.compile(r"(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)")

# `wf-recorder -L` prints e.g. "1. Name: DP-1 Description: AOC 2460G5 0x00033235 (DP-1)"
OUTPUT_LINE = re.compile(r"Name: (\S+)")

ONE_OUTPUT_WARNING = (
    "wf-recorder records one monitor at a time, so this records {name}. "
    "Pick another with --display N (see `yefees-recorder sources`)."
)

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


def on_wayland() -> bool:
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"


class Output(NamedTuple):
    """A Wayland output, under the name wf-recorder's -o takes."""

    name: str
    area: tuple[int, int, int, int] | None  # (x, y, width, height), when known
    focused: bool = False


def list_outputs() -> list[Output]:
    """Wayland outputs in the compositor's own order, which is wf-recorder's too.

    Not from xrandr: under XWayland it lays the monitors out by itself, and here
    put HDMI-A-1 at x=0 where Hyprland has DP-1. Hyprland says where each one is
    and which has focus; other wlroots compositors get names from `wf-recorder -L`.
    """
    try:
        monitors = json.loads(_read(["hyprctl", "monitors", "-j"]) or "[]")
    except ValueError:  # hyprctl outside Hyprland prints an error, not JSON
        monitors = []
    if isinstance(monitors, list) and monitors:
        return [
            Output(m["name"], (m["x"], m["y"], m["width"], m["height"]), bool(m.get("focused")))
            for m in sorted(monitors, key=lambda m: m["id"])
        ]
    lines = _read(["wf-recorder", "-L"]).splitlines()
    return [Output(found.group(1), None) for found in map(OUTPUT_LINE.search, lines) if found]


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
OWNER_MODULE = re.compile(r"Owner Module: (\d+)")

# The sink recordings are routed through while one application is captured.
CAPTURE_SINK = "yefees_capture"

# The sink Wayland mixes system audio and the microphone in, at their gains.
MIX_SINK = "yefees_mix"


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


def _describe(index: int, output: Output) -> str:
    if output.area is None:
        return f"Display {index} - {output.name}"
    x, y, w, h = output.area
    return f"Display {index} - {output.name} {w}x{h} at ({x},{y})"


def list_sources() -> list[Source]:
    if on_wayland():
        # Windows are not offered: wf-recorder can only be pointed at an output
        # or an area, so `--window` is refused there anyway.
        sources = [Source("display", str(i), _describe(i, o)) for i, o in enumerate(list_outputs())]
    else:
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


def find_source(wanted: str, sources: list[str], monitor: bool) -> str:
    """The PulseAudio source `wanted` names: exactly, or by a piece of its name.

    A piece is matched only against the right kind - sink monitors for system
    audio, real inputs for a microphone - because a headset's name is on both.
    With nothing to check against (no pactl) the name is passed on as given.
    """
    if not sources or wanted in sources:
        return wanted
    candidates = [s for s in sources if s.endswith(".monitor") == monitor]
    for source in candidates:
        if wanted.lower() in source.lower():
            return source
    kind = "system audio" if monitor else "microphone"
    raise RuntimeError(
        f"No {kind} source matching {wanted!r}. Available: {', '.join(candidates) or 'none'}."
    )


def _loopback_input(module: str) -> str | None:
    """The sink-input through which a module-loopback plays into its sink."""
    current = None
    for line in _read(["pactl", "list", "sink-inputs"]).splitlines():
        header = SINK_INPUT.search(line)
        if header:
            current = header.group(1)
            continue
        owner = OWNER_MODULE.search(line)
        if owner and owner.group(1) == module:
            return current
    return None


def _unload_leftovers(sink: str) -> None:
    """Unload what a recording killed outright left loaded around `sink`.

    Loopbacks go first, by falling module number: unloaded after the sink, a
    loopback would move on to the default output and play the microphone out loud.
    """
    found = []
    for line in _read(["pactl", "list", "short", "modules"]).splitlines():
        fields = line.split("\t")
        if len(fields) > 2 and sink in fields[2]:
            found.append(fields[0])
    for module in sorted(found, key=int, reverse=True):
        _read(["pactl", "unload-module", module])


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

    def capture_log(self, segment: Path) -> Path:
        # wf-recorder has no quiet switch, and prints a line per format change.
        return segment.with_suffix(".log")

    def _where(self) -> list[str]:
        """The output or area to record. wf-recorder is always told one.

        Left to choose with several monitors connected, it asks on stdin - which
        is our pipe, so it waited there for good and the segment came out empty.
        """
        if self.region:
            x, y, width, height = self.region
            return ["-g", f"{x},{y} {width}x{height}"]
        outputs = list_outputs()
        if self.display is not None:
            if self.display >= len(outputs):
                raise RuntimeError(
                    f"No display {self.display}; the compositor reports {len(outputs)}. "
                    "Run `yefees-recorder sources`."
                )
            return ["-o", outputs[self.display].name]
        if not outputs:
            return []
        chosen = next((o for o in outputs if o.focused), outputs[0])
        if len(outputs) > 1:
            self.video_warning = ONE_OUTPUT_WARNING.format(name=chosen.name)
        return ["-o", chosen.name]

    def _encoder(self) -> list[str]:
        """NVENC when this machine has it, else x264 at the quality's preset.

        Measured at 1080p60 on a GTX 1660 Ti, both holding 60 fps: x264 veryfast
        took 112% of a core, NVENC 19.5%. NVENC is handed the captured bgr0 as
        it is and converts on the GPU; asking wf-recorder for yuv420p first put
        that conversion back on the CPU, at 46%.
        """
        if ffmpeg_can("color=s=256x256,format=bgr0", "-c:v", "h264_nvenc"):
            return ["-c", "h264_nvenc", "-x", "bgr0", "-p", "preset=p4", "-p", "rc=vbr",
                    "-p", f"cq={NVENC_CQ[self.quality]}", "-p", "b=0"]
        return ["-c", "libx264", "-x", "yuv420p",
                "-p", f"preset={self.preset}", "-p", f"crf={self.crf}"]

    def capture_command(self, output: Path) -> list[str]:
        if not shutil.which("wf-recorder"):
            raise RuntimeError(WAYLAND_HELP)
        if self.window is not None:
            raise RuntimeError(
                "Selecting a window is not supported on Wayland - wf-recorder can only "
                "be pointed at a monitor or an area. Use --display or --region."
            )
        if self.app_audio:
            raise RuntimeError(
                "Recording one application is only wired up for X11 so far; on Wayland, "
                "route the app with pactl yourself and pass --audio-device."
            )
        # -D takes a frame every refresh rather than only when the screen
        # changes. Without it, a still screen left SIGINT unanswered until
        # something moved - 17s in one run, past the 15s we wait before killing
        # it - and -r on a still screen wrote an unreadable file.
        command = ["wf-recorder", "-y", "-D", "-r", str(self.fps), *self._where(), *self._encoder()]
        if self._audio_sources:
            source = f"{MIX_SINK}.monitor" if self._mixed else self._audio_sources[0][1]
            command.append(f"--audio={source}")
        return command + ["-f", str(output)]

    @functools.cached_property
    def _audio_sources(self) -> list[tuple[str, str, float]]:
        """(label, PulseAudio source, gain) for each audio source asked for and found."""
        sources = list_audio_sources()
        found = []
        for label, wanted, name, default, gain in (
            ("audio", self.want_audio, self.audio_device, default_monitor_source, self.audio_gain),
            ("mic", self.want_mic, self.mic_device, default_mic_source, self.mic_gain),
        ):
            if not wanted:
                continue
            try:
                source = find_source(name, sources, monitor=label == "audio") if name else default()
            except RuntimeError as exc:
                setattr(self, f"{label}_error", f"{exc} Recording without it.")
                continue
            found.append((label, source, gain))
        return found

    @property
    def _mixed(self) -> bool:
        """Whether the sound has to be mixed before wf-recorder, which takes one
        source and applies no gain."""
        sources = self._audio_sources
        return len(sources) > 1 or any(gain != 1.0 for _, _, gain in sources)

    def setup(self) -> None:
        """Mix the audio sources, at their gains, into a sink wf-recorder records.

        Not recorded beside it and muxed in on stop, as Windows and macOS do:
        on SIGINT wf-recorder drops the frames still in its buffers, and its
        own audio track loses a tail of 13-83ms too, so a wav lined up by
        length came out 50-120ms early. Its own audio carries the clock the
        video starts on; through this mix it stayed within 50ms of the picture.
        """
        self._modules = []
        if not self._mixed:
            return
        if not shutil.which("pactl"):
            raise RuntimeError(
                "Recording system audio and the microphone together, or at a gain other "
                "than 1, needs pactl (Arch: sudo pacman -S libpulse).")
        _unload_leftovers(MIX_SINK)
        try:
            self._modules.append(_pactl(
                ["load-module", "module-null-sink", f"sink_name={MIX_SINK}",
                 f"sink_properties=device.description={MIX_SINK}"]))
            for _, source, gain in self._audio_sources:
                # *_dont_move: if the mix sink vanishes, a loopback unloads
                # rather than move to the speakers and play the microphone.
                # restore-props: WirePlumber keeps stream volumes by name, and
                # the loopbacks' names repeat, so it raced the gain set below
                # (and won once) and handed a gain-1 source the last run's gain.
                module = _pactl(
                    ["load-module", "module-loopback", f"source={source}", f"sink={MIX_SINK}",
                     "latency_msec=20", "source_dont_move=true", "sink_dont_move=true",
                     "sink_input_properties=state.restore-props=false",
                     "source_output_properties=state.restore-props=false"])
                self._modules.append(module)
                if gain != 1.0:
                    sink_input = _loopback_input(module)
                    if sink_input is None:
                        raise RuntimeError(f"Could not find the stream to set {source}'s gain on.")
                    # A decimal point makes pactl read a linear factor, as
                    # the gain is; a bare "2" would be a raw volume of 2/65536.
                    _pactl(["set-sink-input-volume", sink_input, f"{gain:.6f}"])
        except Exception:
            self.teardown()
            raise

    def teardown(self) -> None:
        """Unload the mix, loopbacks first."""
        for module in reversed(getattr(self, "_modules", [])):
            _read(["pactl", "unload-module", module])
        self._modules = []

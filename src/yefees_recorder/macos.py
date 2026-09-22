"""macOS backend.

Video comes from avfoundation's "Capture screen" device. System audio has no
native loopback on macOS, so it needs a virtual output device (BlackHole and
friends) that shows up as an *input* ffmpeg can read; without one, we record
video only rather than failing.

The trap worth knowing: without Screen Recording permission macOS does not
error. Measured on 14.5, the device opens and then never delivers a frame at
all, so ffmpeg sits there forever and writes no file - worse than the black
frames this was once assumed to produce. Permission is therefore checked up
front via CoreGraphics rather than discovered after a ruined recording.
"""

from __future__ import annotations

import ctypes
import re
import subprocess
from pathlib import Path

from .capture import FFMPEG_BASE, AudioInput, CaptureBackend, Source

# Virtual output devices that loop system audio back to an input.
LOOPBACK_DEVICE_HINTS = ("blackhole", "soundflower", "loopback audio", "ishowu", "multi-output")

DEVICE_LINE = re.compile(r"\[(\d+)\]\s+(.+?)\s*$")

# What AVCaptureScreenInput actually hands over. ffmpeg converts it to yuv420p
# for the encoder; asking the device for yuv420p directly is what it refuses.
SCREEN_PIXEL_FORMAT = "uyvy422"

# A resumed segment is never asked for nothing: a duration that is already used
# up would otherwise leave a zero-length file for the concat to choke on.
MINIMUM_SEGMENT = 0.1

# avfoundation hands over audio with honest timestamps but missing samples, so
# the surviving ones have to be put back where they belong instead of being run
# together. Measured on a 6.4s capture: 4.80s of audio without this, 5.98s with
# it, and a beep train 1.000s apart that came out 0.75s apart lands at 1.000s.
AUDIO_GAP_FILLER = "aresample=async=1"

PERMISSION_HELP = (
    "Screen Recording permission has not been granted, so macOS would hand "
    "ffmpeg no frames at all and the recording would hang instead of failing.\n"
    "Grant it in System Settings > Privacy & Security > Screen Recording, tick the "
    "terminal app you are running this from, then run the command again.\n"
    "(macOS only applies the change to newly launched processes, so restart the "
    "terminal if it still fails.)"
)

BLACKHOLE_HELP = (
    "No loopback audio device found, so system audio cannot be captured - macOS "
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


def screen_device_indices(video_devices: dict[int, str]) -> list[int]:
    """avfoundation indices of the screens, in order - index 0 is usually a camera."""
    return [i for i, name in sorted(video_devices.items()) if name.lower().startswith("capture screen")]


def list_sources() -> list[Source]:
    video, audio = list_avfoundation_devices()
    sources = [
        Source("display", str(position), video[index])
        for position, index in enumerate(screen_device_indices(video))
    ]
    for index in sorted(audio):
        name = audio[index]
        loopback = any(hint in name.lower() for hint in LOOPBACK_DEVICE_HINTS)
        sources.append(Source("audio" if loopback else "mic", name,
                              "system audio" if loopback else "microphone"))
    return sources


def segment_seconds(path: Path) -> float:
    """Seconds of video in a finished segment, or 0 when it cannot be read."""
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


def find_microphone(audio_devices: dict[int, str]) -> int | None:
    """The first device that is not a loopback, i.e. a real input."""
    for index, name in sorted(audio_devices.items()):
        if not any(hint in name.lower() for hint in LOOPBACK_DEVICE_HINTS):
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


class AvfAudioRecorder:
    """Records macOS audio to a wav, in an ffmpeg process of its own.

    Two avfoundation inputs cannot share a process. Measured against a beep a
    second: captured on its own the audio keeps all eight beeps and their exact
    spacing, but put the screen in the same ffmpeg and two ragged fragments come
    back - the capture session starves. Two *audio* inputs share a process
    perfectly well, so the split is one process for what is seen and one for
    everything that is heard, and the wav is muxed back in on stop.
    """

    def __init__(self, inputs: list[AudioInput]) -> None:
        self.inputs = inputs
        self._proc: subprocess.Popen | None = None
        self._target: Path | None = None
        self._parts: list[Path] = []

    def parts_for(self, path: Path) -> list[Path]:
        """Where each device is recorded before they are mixed together."""
        return [path.with_name(f"{path.stem}-{number}.wav")
                for number in range(len(self.inputs))]

    def command(self, parts: list[Path]) -> list[str]:
        """The command recording each device to a file of its own.

        Every device gets its own output rather than being mixed on the way in.
        amix cannot keep up with these devices: measured against a beep a
        second, every beep arrives whole when each is written out on its own,
        and comes back as 50 ms fragments with amix between them. Mixing the
        finished files afterwards costs a fraction of a second and keeps them.
        """
        args = list(FFMPEG_BASE)
        for one in self.inputs:
            args += one.args
        for number, (one, part) in enumerate(zip(self.inputs, parts)):
            steps = [AUDIO_GAP_FILLER]
            if one.gain != 1.0:
                steps.append(f"volume={one.gain}")
            args += ["-map", f"{number}:a", "-af", ",".join(steps),
                     "-c:a", "pcm_s16le", str(part)]
        return args

    def mix_command(self, parts: list[Path], path: Path) -> list[str]:
        """The command folding the finished per-device files into one."""
        args = list(FFMPEG_BASE)
        for part in parts:
            args += ["-i", str(part)]
        taps = "".join(f"[{number}:a]" for number in range(len(parts)))
        # normalize=0 for the reason it is needed everywhere else: amix scales
        # every input by 1/n unless told not to, so switching the microphone on
        # would quieten the system audio.
        return args + [
            "-filter_complex",
            f"{taps}amix=inputs={len(parts)}:duration=longest:normalize=0[out]",
            "-map", "[out]", "-c:a", "pcm_s16le", str(path),
        ]

    def start(self, path: Path) -> None:
        self._target = Path(path)
        self._parts = self.parts_for(self._target)
        self._proc = subprocess.Popen(self.command(self._parts), stdin=subprocess.PIPE)

    def stop(self) -> None:
        if self._proc is not None:
            try:
                self._proc.stdin.write(b"q")  # ffmpeg finalizes the wavs on "q"
                self._proc.stdin.flush()
                self._proc.wait(timeout=15)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                self._proc.kill()
                self._proc.wait()
            finally:
                self._proc = None
        self._combine()

    def _combine(self) -> None:
        """Leave one wav where the caller asked for it, whatever was recorded.

        A device that produced nothing is dropped rather than silencing the
        others, and no recording at all leaves no file, which the mux reads as
        "video only".
        """
        if self._target is None:
            return
        usable = [p for p in self._parts if p.exists() and p.stat().st_size > 0]
        self._parts = []
        if not usable:
            return
        if len(usable) == 1:
            usable[0].replace(self._target)
            return
        subprocess.run(self.mix_command(usable, self._target), capture_output=True)


class MacBackend(CaptureBackend):
    # ffmpeg counts -t from the first frame it captures, and avfoundation takes
    # 1.3s to hand that over - a duration timed from launch loses every one of
    # those seconds, measured as 4.0s of video for a 5s recording. Nothing
    # outside ffmpeg can see when the device woke up, so ffmpeg is asked to do
    # the counting.
    limits_duration = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._devices = None

    @property
    def devices(self) -> tuple[dict[int, str], dict[int, str]]:
        if self._devices is None:
            self._devices = list_avfoundation_devices()
        return self._devices

    def output_options(self) -> list[str]:
        if not self.duration:
            return []
        # After a pause the next segment asks for what is left of the duration;
        # handing it the whole of it again would overrun the recording.
        done = sum(segment_seconds(video) for video, _ in self._segments)
        return ["-t", f"{max(self.duration - done, MINIMUM_SEGMENT):.3f}"]

    def video_filters(self) -> list[str]:
        # avfoundation grabs a whole screen, so a region has to be cropped after.
        if not self.region:
            return super().video_filters()
        x, y, width, height = self.region
        return [f"crop={width}:{height}:{x}:{y}", *super().video_filters()]

    def video_input_args(self) -> list[str]:
        if self.window:
            raise RuntimeError(
                "avfoundation cannot record a single window - it only exposes whole "
                "screens. Use --region to crop to the window's area instead."
            )
        if screen_recording_permitted() is False:
            request_screen_recording()  # surfaces the system prompt
            raise RuntimeError(PERMISSION_HELP)
        screens = screen_device_indices(self.devices[0])
        if not screens:
            raise RuntimeError(
                "ffmpeg found no 'Capture screen' device. This usually means Screen "
                "Recording permission is missing for this terminal."
            )
        if self.display is not None and self.display >= len(screens):
            raise RuntimeError(
                f"No display {self.display}; this Mac exposes {len(screens)}. "
                "Run `yefees-recorder sources`."
            )
        screen = screens[self.display or 0]
        return [
            "-f", "avfoundation",
            "-framerate", str(self.fps),
            # The demuxer asks for yuv420p by default, which no screen device
            # offers, and ffmpeg then prints its fallback at *error* level on
            # every single recording. Naming the format it would have settled on
            # keeps the output quiet; measured, it costs nothing (1.30s to the
            # first frame either way, against 1.39s for nv12).
            "-pixel_format", SCREEN_PIXEL_FORMAT,
            "-capture_cursor", "1",
            "-i", f"{screen}:none",
        ]

    def _named_device(self, wanted: str) -> int:
        devices = self.devices[1]
        found = next(
            (i for i, name in sorted(devices.items()) if wanted.lower() in name.lower()), None
        )
        if found is None:
            raise RuntimeError(
                f"No audio device matching {wanted!r}. Run `yefees-recorder sources`."
            )
        return found

    def setup(self) -> None:
        if self.app_audio:
            raise RuntimeError(
                "macOS cannot capture one application's audio - Core Audio process "
                "taps are not reachable through ffmpeg.\n"
                "Route the app to a virtual output device (BlackHole) in its own "
                "settings or with a tool like Loopback, then use --audio-device."
            )

    def audio_inputs(self) -> list[AudioInput]:
        # Nothing is recorded alongside the screen: a second avfoundation input
        # in that process starves the capture session. See AvfAudioRecorder.
        return []

    def side_audio_filters(self) -> list[str]:
        # The recorder already mixed its devices at their own levels, so there
        # is no gain left to apply to the wav.
        return []

    def audio_lead(self, video: Path, audio: Path) -> float:
        """How far ahead of the first frame the wav starts.

        avfoundation opens an audio device in a fraction of the 1.3s the screen
        takes, and the recorder is started first, so the wav begins well before
        the video does - measured at 0.74s, which is plainly audible. Both are
        stopped within milliseconds of each other, so whatever length the wav
        has over the video it has at the front. It is not a constant: it depends
        on how fast each device happens to wake up.
        """
        return max(segment_seconds(audio) - segment_seconds(video), 0.0)

    def make_audio_recorder(self):
        sources = self.audio_sources()
        return AvfAudioRecorder(sources) if sources else None

    def audio_sources(self) -> list[AudioInput]:
        """The avfoundation audio devices to record, system audio first.

        Separate inputs rather than the combined "screen:audio" form, which
        drifts between the streams.
        """
        inputs = []
        if self.want_audio:
            if self.audio_device:
                loopback = self._named_device(self.audio_device)
            else:
                loopback = find_loopback_device(self.devices[1])
            if loopback is None:
                self.audio_error = BLACKHOLE_HELP
            else:
                inputs.append(AudioInput(
                    ["-f", "avfoundation", "-i", f"none:{loopback}"], self.audio_gain))
        if self.want_mic:
            if self.mic_device:
                mic = self._named_device(self.mic_device)
            else:
                mic = find_microphone(self.devices[1])
            if mic is None:
                self.mic_error = "No microphone found; recording without it."
            else:
                inputs.append(AudioInput(
                    ["-f", "avfoundation", "-i", f"none:{mic}"], self.mic_gain))
        return inputs

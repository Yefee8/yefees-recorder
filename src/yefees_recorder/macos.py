"""macOS backend.

Video comes from avfoundation's "Capture screen" device; audio comes from
CoreAudio through PortAudio, because ffmpeg 8.1's avfoundation drops audio
buffers. System audio has no native loopback on macOS, so it needs a virtual
output device (BlackHole and friends) that shows up as an *input*; without one,
we record video only rather than failing.

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
import wave
from pathlib import Path

from .capture import CaptureBackend, MixedRecorder, Source, segment_seconds

# Virtual output devices that loop system audio back to an input.
LOOPBACK_DEVICE_HINTS = ("blackhole", "soundflower", "loopback audio", "ishowu", "multi-output")

DEVICE_LINE = re.compile(r"\[(\d+)\]\s+(.+?)\s*$")

# What AVCaptureScreenInput actually hands over. ffmpeg converts it to yuv420p
# for the encoder; asking the device for yuv420p directly is what it refuses.
SCREEN_PIXEL_FORMAT = "uyvy422"

# A resumed segment is never asked for nothing: a duration that is already used
# up would otherwise leave a zero-length file for the concat to choke on.
MINIMUM_SEGMENT = 0.1

PERMISSION_HELP = (
    "Screen Recording permission has not been granted, so macOS would hand "
    "ffmpeg no frames at all and the recording would hang instead of failing.\n"
    "Grant it in System Settings > Privacy & Security > Screen Recording, tick the "
    "terminal app you are running this from, then run the command again.\n"
    "(macOS only applies the change to newly launched processes, so restart the "
    "terminal if it still fails.)"
)

MICROPHONE_HELP = (
    "This terminal has no Microphone permission, so this recording has no sound "
    "- macOS would hand over silence rather than refuse. The screen is still "
    "being captured.\n"
    "Every audio input needs Microphone permission, virtual devices like "
    "BlackHole included. Grant it in System Settings > Privacy & Security > "
    "Microphone for the terminal you are running this from, then restart the "
    "terminal - macOS only applies the change to newly launched processes."
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


def list_audio_inputs() -> dict[int, str]:
    """CoreAudio inputs as {PortAudio index: name} - the names avfoundation lists."""
    import sounddevice

    return {index: device["name"] for index, device in enumerate(sounddevice.query_devices())
            if device["max_input_channels"] > 0}


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


def microphone_permitted() -> bool | None:
    """True, False, or None when macOS has not asked yet or cannot be asked.

    Needed because CoreAudio does not refuse an input it may not read: it hands
    over silence, and a silent recording nobody was told about is the worst
    outcome there is. AVMediaTypeAudio is the string "soun".
    """
    try:
        objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AVFoundation.framework/AVFoundation")
        for name in ("objc_getClass", "sel_registerName"):
            getattr(objc, name).restype = ctypes.c_void_p
            getattr(objc, name).argtypes = [ctypes.c_char_p]
        # objc_msgSend must be called through the exact prototype on arm64.
        send = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
        to_string = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)(send)
        status_of = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(send)
        media = to_string(objc.objc_getClass(b"NSString"), objc.sel_registerName(b"stringWithUTF8String:"), b"soun")
        status = status_of(objc.objc_getClass(b"AVCaptureDevice"),
                           objc.sel_registerName(b"authorizationStatusForMediaType:"), media)
    except (OSError, AttributeError):
        return None
    # AVAuthorizationStatus: 0 not determined, 1 restricted, 2 denied, 3 authorized
    return {1: False, 2: False, 3: True}.get(status)


def request_screen_recording() -> bool:
    """Ask macOS to show the Screen Recording permission prompt."""
    try:
        request = _coregraphics().CGRequestScreenCaptureAccess
        request.restype = ctypes.c_bool
        return bool(request())
    except (OSError, AttributeError):
        return False


class SounddeviceRecorder:
    """One CoreAudio input to a wav, through PortAudio rather than ffmpeg.

    ffmpeg 8.1's avfoundation keeps a single audio buffer and throws it away
    whenever the next one lands before it was read, and ffmpeg only looks every
    10 ms when it finds nothing waiting. Buffers went missing - 6.4s of capture
    held 4.8s of audio - and the holes came back as silence: the stutter. Fixed
    upstream in July 2026 (ddf8f40), in no release yet. PortAudio's callback is
    handed every buffer.
    """

    # ponytail: writes in the audio callback and does not pad dropouts against
    # the clock as the Windows recorder must; CoreAudio delivers continuously.
    # Add a queue and a writer thread if overflows ever show up.

    def __init__(self, device: int) -> None:
        self.device = device
        self._stream = None
        self._wav = None

    def start(self, path: Path) -> None:
        import sounddevice

        info = sounddevice.query_devices(self.device)
        channels = min(int(info["max_input_channels"]), 2)
        rate = int(info["default_samplerate"])
        self._wav = wave.open(str(path), "wb")
        self._wav.setnchannels(channels)
        self._wav.setsampwidth(2)  # int16
        self._wav.setframerate(rate)
        self._stream = sounddevice.RawInputStream(
            device=self.device, channels=channels, samplerate=rate, dtype="int16",
            callback=lambda data, frames, when, status: self._wav.writeframes(bytes(data)),
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()  # waits for the last callback
            self._stream.close()
            self._stream = None
        if self._wav is not None:
            self._wav.close()
            self._wav = None


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

    @staticmethod
    def _named_device(wanted: str, devices: dict[int, str]) -> int:
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

    def make_audio_recorder(self):
        wanted = [label for label, on in (("audio", self.want_audio), ("mic", self.want_mic)) if on]
        if microphone_permitted() is False:
            for label in wanted:
                setattr(self, f"{label}_error", MICROPHONE_HELP)
            return None
        try:
            devices = self.audio_devices()
        except (ImportError, OSError) as exc:  # sounddevice or PortAudio missing
            for label in wanted:
                setattr(self, f"{label}_error", f"Cannot reach CoreAudio ({exc}); recording without sound.")
            return None
        return MixedRecorder(devices) if devices else None

    def audio_devices(self) -> list[tuple[str, SounddeviceRecorder, float]]:
        """The inputs to record, system audio first, as (label, recorder, gain).

        Kept out of the screen's process: measured, a second avfoundation input
        in it starved the capture session and eight beeps came back as two
        ragged fragments.
        """
        inputs = list_audio_inputs()
        found = []
        if self.want_audio:
            if self.audio_device:
                loopback = self._named_device(self.audio_device, inputs)
            else:
                loopback = find_loopback_device(inputs)
            if loopback is None:
                self.audio_error = BLACKHOLE_HELP
            else:
                found.append(("audio", SounddeviceRecorder(loopback), self.audio_gain))
        if self.want_mic:
            if self.mic_device:
                mic = self._named_device(self.mic_device, inputs)
            else:
                mic = find_microphone(inputs)
            if mic is None:
                self.mic_error = "No microphone found; recording without it."
            else:
                found.append(("mic", SounddeviceRecorder(mic), self.mic_gain))
        return found

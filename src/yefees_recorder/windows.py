"""Windows backend: ddagrab or gdigrab for video, WASAPI for all audio.

dshow exposes no loopback device, so system audio cannot come from ffmpeg here,
and a dshow microphone in the capture process stalls the screen grab. So
PyAudioWPatch records both, each to its own wav, mixed and muxed in afterwards.
"""

from __future__ import annotations

import ctypes
import functools
import subprocess
import time
import wave
from ctypes import wintypes
from pathlib import Path

from .capture import FFMPEG_BASE, CaptureBackend, MixedRecorder, Source

DWMWA_CLOAKED = 14  # set on UWP windows that exist but are not really on screen

# Windows feeds a loopback stream only while something is actually playing - it
# goes quiet mid-recording and may never fire at all on a silent machine. So the
# wav is built against wall clock: every callback drops its data at the position
# the clock says it belongs, and the gaps are filled with real silence.
GAP_TOLERANCE = 0.01

# NVENC constant-quality levels matched to the x264 settings each quality name
# used to mean, by VMAF on 1080p60 gameplay. See CLAUDE.md, "Windows capture".
NVENC_CQ = {"low": 42, "balanced": 36, "high": 27}

SLOW_DESKTOP_WARNING = (
    "Recording every monitor at once has to go through GDI, which is slow - "
    "expect well under 60 fps and a busy CPU. Pass --display N to record one "
    "monitor through the GPU instead."
)


class _DxgiOutputDesc(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_wchar * 32),
        ("rect", wintypes.RECT),
        ("attached", wintypes.BOOL),
        ("rotation", ctypes.c_uint),
        ("monitor", wintypes.HMONITOR),
    ]


def _com_method(obj, index: int, *argtypes):
    table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(table[index])


def list_dxgi_outputs() -> list[tuple[int, int, int, int]]:
    """Each output ddagrab can open, as (x, y, width, height), by its output_idx.

    ddagrab numbers the outputs of the default adapter and says nothing about
    which monitor an index is - and the order is not EnumDisplayMonitors'
    either: here output 0 is \\\\.\\DISPLAY2. So they are matched by position.
    """
    guid = (ctypes.c_ubyte * 16).from_buffer_copy(  # IID_IDXGIFactory1
        bytes.fromhex("78ae0a776ff2ba4da829253c83d1b387"))
    factory, adapter = ctypes.c_void_p(), ctypes.c_void_p()
    if ctypes.windll.dxgi.CreateDXGIFactory1(ctypes.byref(guid), ctypes.byref(factory)):
        return []
    outputs: list[tuple[int, int, int, int]] = []
    try:
        # vtable slot 7 is EnumAdapters on the factory and EnumOutputs on an
        # adapter, GetDesc on an output; slot 2 is Release on all of them.
        if _com_method(factory, 7, ctypes.c_uint, ctypes.c_void_p)(factory, 0, ctypes.byref(adapter)):
            return []
        while True:
            output = ctypes.c_void_p()
            if _com_method(adapter, 7, ctypes.c_uint, ctypes.c_void_p)(
                    adapter, len(outputs), ctypes.byref(output)):
                break
            desc = _DxgiOutputDesc()
            _com_method(output, 7, ctypes.c_void_p)(output, ctypes.byref(desc))
            _com_method(output, 2)(output)
            area = desc.rect
            outputs.append((area.left, area.top, area.right - area.left, area.bottom - area.top))
        _com_method(adapter, 2)(adapter)
    finally:
        _com_method(factory, 2)(factory)
    return outputs


@functools.lru_cache(maxsize=None)
def ffmpeg_can(source: str, *codec: str) -> bool:
    """Whether this machine's ffmpeg can push one frame of `source` through `codec`.

    The only honest test for a hardware path: ddagrab needs ffmpeg 6 and a real
    session, NVENC needs an NVIDIA GPU and driver, and neither says so up front.
    """
    try:
        return subprocess.run(
            FFMPEG_BASE + ["-f", "lavfi", "-i", source, "-frames:v", "1", *codec, "-f", "null", "-"],
            capture_output=True, timeout=15,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def list_monitors() -> list[tuple[int, int, int, int]]:
    """Each monitor as (x, y, width, height) in virtual-desktop coordinates.

    Offsets can be negative when a monitor sits left of or above the primary.
    """
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM,
    )
    monitors: list[tuple[int, int, int, int]] = []

    def on_monitor(handle, device_context, rect, data):
        area = rect.contents
        monitors.append((area.left, area.top, area.right - area.left, area.bottom - area.top))
        return True

    user32.EnumDisplayMonitors(0, None, callback_type(on_monitor), 0)
    return monitors


def list_window_titles() -> list[str]:
    """Titles of real, on-screen windows.

    Cloaked windows are skipped: Windows keeps suspended UWP apps (Settings,
    Movies & TV) marked visible, so without this the list fills with duplicates
    of apps that aren't on screen.
    """
    user32, dwmapi = ctypes.windll.user32, ctypes.windll.dwmapi
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    titles: list[str] = []

    def on_window(handle, data):
        if not user32.IsWindowVisible(handle):
            return True
        length = user32.GetWindowTextLengthW(handle)
        if not length:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        title = buffer.value.strip()
        cloaked = ctypes.c_int(0)
        dwmapi.DwmGetWindowAttribute(
            handle, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        )
        if title and not cloaked.value and title != "Program Manager":
            titles.append(title)
        return True

    user32.EnumWindows(callback_type(on_window), 0)
    return titles


def _wasapi_devices(audio, pyaudio, loopback: bool) -> list[dict]:
    """WASAPI capture devices: loopbacks (what is playing) or microphones."""
    if loopback:
        return list(audio.get_loopback_device_info_generator())
    host = audio.get_host_api_info_by_type(pyaudio.paWASAPI)["index"]
    devices = (audio.get_device_info_by_index(i) for i in range(audio.get_device_count()))
    return [d for d in devices if d["hostApi"] == host and d["maxInputChannels"] > 0
            and not d.get("isLoopbackDevice")]


def _list_wasapi(loopback: bool) -> list[str]:
    try:
        import pyaudiowpatch as pyaudio
    except ImportError:
        return []
    audio = pyaudio.PyAudio()
    try:
        return [device["name"] for device in _wasapi_devices(audio, pyaudio, loopback)]
    except Exception:
        return []
    finally:
        audio.terminate()


def list_loopback_devices() -> list[str]:
    return _list_wasapi(loopback=True)


def list_microphones() -> list[str]:
    """Microphones WASAPI can open - the same endpoint names dshow lists."""
    return _list_wasapi(loopback=False)


def list_sources() -> list[Source]:
    sources = [
        Source("display", str(index), f"Display {index} - {w}x{h} at ({x},{y})")
        for index, (x, y, w, h) in enumerate(list_monitors())
    ]
    sources += [Source("window", title, title) for title in list_window_titles()]
    sources += [Source("audio", name, name) for name in list_loopback_devices()]
    sources += [Source("mic", name, name) for name in list_microphones()]
    return sources


class WasapiRecorder:
    """Records one WASAPI device to a wav: a loopback of what is playing, or a mic."""

    def __init__(self, device_name: str | None = None, loopback: bool = True) -> None:
        import pyaudiowpatch as pyaudio

        self._pyaudio = pyaudio
        self._pa = pyaudio.PyAudio()
        try:
            self._device = self._find_device(device_name, loopback)
        except Exception:
            self._pa.terminate()
            raise
        self.channels = self._device["maxInputChannels"]
        self.rate = int(self._device["defaultSampleRate"])
        self._stream = None
        self._wav = None
        self._frames_written = 0
        self._start_time = None

    def _find_device(self, device_name: str | None, loopback: bool):
        if device_name is None and loopback:
            return self._pa.get_default_wasapi_loopback()
        if device_name is None:
            host = self._pa.get_host_api_info_by_type(self._pyaudio.paWASAPI)
            if host["defaultInputDevice"] < 0:
                raise RuntimeError("No microphone found.")
            return self._pa.get_device_info_by_index(host["defaultInputDevice"])
        devices = _wasapi_devices(self._pa, self._pyaudio, loopback)
        wanted = device_name.lower()
        for device in devices:
            if wanted in device["name"].lower():
                return device
        kind = "loopback device" if loopback else "microphone"
        available = ", ".join(d["name"] for d in devices)
        raise RuntimeError(f"No {kind} matching {device_name!r}. Available: {available}")

    def _silence(self, frames: int) -> bytes:
        return b"\x00" * (frames * self.channels * 2)  # paInt16

    def _elapsed_frames(self) -> int:
        return int((time.monotonic() - self._start_time) * self.rate)

    def start(self, path: Path) -> None:
        self._wav = wave.open(str(path), "wb")
        self._wav.setnchannels(self.channels)
        self._wav.setsampwidth(self._pa.get_sample_size(self._pyaudio.paInt16))
        self._wav.setframerate(self.rate)

        def on_frames(in_data, frame_count, time_info, status):
            missing = self._elapsed_frames() - self._frames_written - frame_count
            if missing > self.rate * GAP_TOLERANCE:
                self._wav.writeframes(self._silence(missing))
                self._frames_written += missing
            self._wav.writeframes(in_data)
            self._frames_written += frame_count
            return (in_data, self._pyaudio.paContinue)

        self._stream = self._pa.open(
            format=self._pyaudio.paInt16,
            channels=self.channels,
            rate=self.rate,
            input=True,
            input_device_index=self._device["index"],
            frames_per_buffer=1024,
            stream_callback=on_frames,
        )
        self._start_time = time.monotonic()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        if self._wav is not None:
            if self._start_time is not None:  # pad a silent tail up to full length
                missing = self._elapsed_frames() - self._frames_written
                if missing > 0:
                    self._wav.writeframes(self._silence(missing))
                    self._frames_written += missing
            self._wav.close()
            self._wav = None
        self._pa.terminate()


class WindowsBackend(CaptureBackend):
    def _display_region(self) -> tuple[int, int, int, int] | None:
        if self.display is None:
            return None
        monitors = list_monitors()
        if self.display >= len(monitors):
            raise RuntimeError(
                f"No display {self.display}; this machine has {len(monitors)} "
                f"(0–{len(monitors) - 1}). Run `yefees-recorder sources`."
            )
        return monitors[self.display]

    def _ddagrab(self) -> str | None:
        """A ddagrab source for what was asked, or None when only gdigrab can do it.

        ddagrab sees one monitor at a time, so a window - and any area spanning
        monitors, the whole desktop of a multi-monitor machine included - stays
        on gdigrab.
        """
        if self.window:
            return None
        source = f"ddagrab=framerate={self.fps}"
        outputs = list_dxgi_outputs()
        area = self.region or self._display_region()
        if area is None:
            if len(outputs) == 1 == len(list_monitors()):
                return source  # the whole desktop is that one output
            if len(outputs) > 1:
                self.video_warning = SLOW_DESKTOP_WARNING
            return None
        x, y, width, height = area
        for index, (left, top, w, h) in enumerate(outputs):
            if left <= x and top <= y and x + width <= left + w and y + height <= top + h:
                # Even, for the encoders' 4:2:0; a GPU frame cannot be padded.
                return (f"{source}:output_idx={index}:offset_x={x - left}:offset_y={y - top}"
                        f":video_size={width - width % 2}x{height - height % 2}")
        return None

    @functools.cached_property
    def _capture_path(self) -> tuple[str | None, bool]:
        """(ddagrab source, or None for gdigrab; whether NVENC encodes it).

        Measured at 1080p60 on a GTX 1660 Ti: gdigrab into x264 reached 35 fps on
        170% of a core, ddagrab into NVENC 55-58 fps on 4.5%. ddagrab into x264
        sits between, for a machine without NVENC.
        """
        grab = self._ddagrab()
        if grab and ffmpeg_can(grab, "-c:v", "h264_nvenc"):
            return grab, True
        if grab and ffmpeg_can(grab + ",hwdownload,format=bgra"):
            return grab + ",hwdownload,format=bgra", False
        return None, False

    def video_filters(self) -> list[str]:
        # NVENC takes the GPU frame as it is; a software filter cannot touch it.
        return [] if self._capture_path[1] else super().video_filters()

    def video_codec_args(self) -> list[str]:
        if not self._capture_path[1]:
            return super().video_codec_args()
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr",
                "-cq", str(NVENC_CQ[self.quality]), "-b:v", "0"]

    def video_input_args(self) -> list[str]:
        grab = self._capture_path[0]
        if grab:
            return ["-f", "lavfi", "-i", grab]
        args = ["-f", "gdigrab", "-framerate", str(self.fps)]
        if self.window:
            # gdigrab targets a window by title natively, so no region maths.
            return args + ["-i", f"title={self.window}"]
        region = self.region or self._display_region()
        if region:
            x, y, width, height = region
            args += ["-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{width}x{height}"]
        return args + ["-i", "desktop"]

    def setup(self) -> None:
        if not self.app_audio:
            return
        outputs = ", ".join(list_loopback_devices()) or "none found"
        raise RuntimeError(
            "Windows has no way to capture one application's audio directly - that "
            "needs the process-loopback API, which ffmpeg does not expose.\n"
            "What does work: send the app to its own output device in Settings > "
            "System > Sound > Volume mixer, then record that device with "
            "--audio-device.\n"
            f"Output devices available here: {outputs}"
        )

    def make_audio_recorder(self):
        """System audio and the microphone, both through WASAPI, mixed on stop.

        The microphone used to be a dshow input in the capture process, and that
        alone held 60 fps down to 20-24: dshow hands audio over in 500 ms blocks
        and ffmpeg stalls the screen grab until each one arrives. Measured with
        ddagrab: 59.5 fps alone, 24 fps beside the mic, no better than 52 with
        the smallest audio_buffer_size.
        """
        devices = []
        for label, wanted, name, loopback, gain in (
            ("audio", self.want_audio, self.audio_device, True, self.audio_gain),
            ("mic", self.want_mic, self.mic_device, False, self.mic_gain),
        ):
            if not wanted:
                continue
            try:
                devices.append((label, WasapiRecorder(name, loopback), gain))
            except Exception as exc:  # no such device, or PyAudioWPatch missing
                setattr(self, f"{label}_error", f"{exc} Recording without it.")
        return MixedRecorder(devices) if devices else None

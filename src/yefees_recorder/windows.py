"""Windows backend: gdigrab for video, WASAPI loopback for system audio.

dshow exposes no loopback device, so system audio cannot come from ffmpeg here —
PyAudioWPatch captures it natively and the wav is muxed in afterwards.
"""

from __future__ import annotations

import ctypes
import time
import wave
from ctypes import wintypes
from pathlib import Path

from .capture import CaptureBackend, Source

DWMWA_CLOAKED = 14  # set on UWP windows that exist but are not really on screen

# Windows feeds a loopback stream only while something is actually playing — it
# goes quiet mid-recording and may never fire at all on a silent machine. So the
# wav is built against wall clock: every callback drops its data at the position
# the clock says it belongs, and the gaps are filled with real silence.
GAP_TOLERANCE = 0.01


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


def list_loopback_devices() -> list[str]:
    try:
        import pyaudiowpatch as pyaudio
    except ImportError:
        return []
    audio = pyaudio.PyAudio()
    try:
        return [device["name"] for device in audio.get_loopback_device_info_generator()]
    except Exception:
        return []
    finally:
        audio.terminate()


def list_sources() -> list[Source]:
    sources = [
        Source("display", str(index), f"Display {index} — {w}x{h} at ({x},{y})")
        for index, (x, y, w, h) in enumerate(list_monitors())
    ]
    sources += [Source("window", title, title) for title in list_window_titles()]
    sources += [Source("audio", name, name) for name in list_loopback_devices()]
    return sources


class WasapiLoopbackRecorder:
    """Records whatever the default output device is playing, to a wav file."""

    def __init__(self, device_name: str | None = None) -> None:
        import pyaudiowpatch as pyaudio

        self._pyaudio = pyaudio
        self._pa = pyaudio.PyAudio()
        try:
            self._device = self._find_device(device_name)
        except Exception:
            self._pa.terminate()
            raise
        self.channels = self._device["maxInputChannels"]
        self.rate = int(self._device["defaultSampleRate"])
        self._stream = None
        self._wav = None
        self._frames_written = 0
        self._start_time = None

    def _find_device(self, device_name: str | None):
        if device_name is None:
            return self._pa.get_default_wasapi_loopback()
        wanted = device_name.lower()
        for device in self._pa.get_loopback_device_info_generator():
            if wanted in device["name"].lower():
                return device
        available = ", ".join(d["name"] for d in self._pa.get_loopback_device_info_generator())
        raise RuntimeError(f"No loopback device matching {device_name!r}. Available: {available}")

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

    def video_input_args(self) -> list[str]:
        args = ["-f", "gdigrab", "-framerate", str(self.fps)]
        if self.window:
            # gdigrab targets a window by title natively, so no region maths.
            return args + ["-i", f"title={self.window}"]
        region = self.region or self._display_region()
        if region:
            x, y, width, height = region
            args += ["-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{width}x{height}"]
        return args + ["-i", "desktop"]

    def make_audio_recorder(self):
        try:
            return WasapiLoopbackRecorder(self.audio_device)
        except Exception as exc:  # no loopback device, or PyAudioWPatch missing
            self.audio_error = str(exc)
            return None

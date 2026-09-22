"""Windows backend: gdigrab for video, WASAPI loopback for system audio.

dshow exposes no loopback device, so system audio cannot come from ffmpeg here —
PyAudioWPatch captures it natively and the wav is muxed in afterwards.
"""

from __future__ import annotations

import time
import wave
from pathlib import Path

from .capture import CaptureBackend

# Windows feeds a loopback stream only while something is actually playing — it
# goes quiet mid-recording and may never fire at all on a silent machine. So the
# wav is built against wall clock: every callback drops its data at the position
# the clock says it belongs, and the gaps are filled with real silence.
GAP_TOLERANCE = 0.01


class WasapiLoopbackRecorder:
    """Records whatever the default output device is playing, to a wav file."""

    def __init__(self) -> None:
        import pyaudiowpatch as pyaudio

        self._pyaudio = pyaudio
        self._pa = pyaudio.PyAudio()
        try:
            self._device = self._pa.get_default_wasapi_loopback()
        except Exception:
            self._pa.terminate()
            raise
        self.channels = self._device["maxInputChannels"]
        self.rate = int(self._device["defaultSampleRate"])
        self._stream = None
        self._wav = None
        self._frames_written = 0
        self._start_time = None

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
    def video_input_args(self) -> list[str]:
        return ["-f", "gdigrab", "-framerate", str(self.fps), "-i", "desktop"]

    def make_audio_recorder(self):
        try:
            return WasapiLoopbackRecorder()
        except Exception as exc:  # no loopback device, or PyAudioWPatch missing
            self.audio_error = str(exc)
            return None

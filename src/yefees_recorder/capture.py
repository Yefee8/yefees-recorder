"""Capture engine.

ffmpeg does the capturing and encoding; each platform backend only supplies the
input arguments for its screen-grab device and (optionally) a system-audio
recorder. Pause works by ending the current ffmpeg segment and starting a new
one on resume; segments are muxed with their audio and concatenated on stop.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

FFMPEG_BASE = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]

# gdigrab/x11grab can hand back odd dimensions, which yuv420p cannot encode.
EVEN_DIMS = "pad=ceil(iw/2)*2:ceil(ih/2)*2"

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
        preset: str = "ultrafast",
        audio_offset: float = 0.0,
    ) -> None:
        self.output = Path(output)
        self.fps = fps
        self.want_audio = audio
        self.preset = preset
        # ponytail: fixed A/V nudge in seconds; per-machine calibration knob.
        # Raise it if audio runs early, lower it if audio runs late.
        self.audio_offset = audio_offset
        self._tmpdir = tempfile.TemporaryDirectory(prefix="yefees-")
        self.workdir = Path(self._tmpdir.name)
        self._segments: list[tuple[Path, Path | None]] = []
        self._proc: subprocess.Popen | None = None
        self._audio = None

    @abstractmethod
    def video_input_args(self) -> list[str]:
        """ffmpeg args opening this platform's screen capture device."""

    def make_audio_recorder(self):
        """A system-audio recorder, or None when the platform has no usable one."""
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
        if self.want_audio:
            recorder = self.make_audio_recorder()
            if recorder is not None:
                audio = self.workdir / f"seg{index}.wav"
                recorder.start(audio)
                self._audio = recorder
        self._proc = subprocess.Popen(
            FFMPEG_BASE
            + self.video_input_args()
            + ["-vf", EVEN_DIMS, "-c:v", "libx264", "-preset", self.preset,
               "-pix_fmt", "yuv420p", str(video)],
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
            self._proc.stdin.write(b"q")  # graceful stop: ffmpeg finalizes the file
            self._proc.stdin.flush()
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
        _run(["-i", str(video), *offset, "-i", str(audio),
              "-c:v", "copy", "-c:a", "aac", str(merged)])
        return merged

    def _concat(self, parts: list[Path]) -> None:
        listing = self.workdir / "segments.txt"
        listing.write_text("\n".join(f"file '{p.as_posix()}'" for p in parts), encoding="utf-8")
        _run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(self.output)])


def get_backend(output: Path, **kwargs) -> CaptureBackend:
    """The recorder for the current OS."""
    import platform

    system = platform.system()
    if system == "Windows":
        from .windows import WindowsBackend

        return WindowsBackend(output, **kwargs)
    raise NotImplementedError(
        f"{system} backend is not implemented yet (phases 1a/1b) — Windows only for now."
    )

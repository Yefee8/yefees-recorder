"""Real X11 capture, exercised on Linux with a display (CI supplies Xvfb).

This is the only place x11grab actually runs. A recorder can produce a
perfectly valid file full of black frames, so this checks pixel content and
not just that a file appeared.
"""

import os
import platform
import shutil
import subprocess
import time

import pytest

from yefees_recorder.linux import LinuxX11Backend

pytestmark = pytest.mark.skipif(
    platform.system() != "Linux" or not os.environ.get("DISPLAY"),
    reason="needs Linux with a display",
)


def probe(path, entries):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         entries, "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.split()


def average_rgb(path):
    """Mean colour of one frame, by letting ffmpeg scale it down to 1x1."""
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", "1", "-i", str(path), "-frames:v", "1",
         "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    )
    return tuple(result.stdout[:3])


def test_x11grab_records_the_actual_display(tmp_path):
    painted = bool(shutil.which("xsetroot")) and (
        subprocess.run(["xsetroot", "-solid", "#c81e1e"]).returncode == 0
    )

    backend = LinuxX11Backend(tmp_path / "out.mkv", fps=10, audio=False)
    backend.start()
    time.sleep(3)
    output = backend.stop()

    width, height = (int(v) for v in probe(output, "stream=width,height"))
    assert width > 1 and height > 1, "x11grab attached to no display geometry"
    assert 2 < float(probe(output, "format=duration")[0]) < 4.5

    if painted:
        red, green, blue = average_rgb(output)
        assert red > green + 40 and red > blue + 40, (
            f"expected the painted red root window, captured {(red, green, blue)} "
            "— x11grab produced blank frames"
        )

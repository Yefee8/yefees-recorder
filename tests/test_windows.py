"""Windows loopback audio. The silent-machine case is the one that used to
break: WASAPI stops delivering when nothing plays, so the wav has to be padded
up to wall clock or it drifts out of sync with the video."""

import platform
import time
import wave

import pytest

pytestmark = pytest.mark.skipif(platform.system() != "Windows", reason="Windows only")


@pytest.fixture
def recorder():
    from yefees_recorder.windows import WasapiLoopbackRecorder

    try:
        return WasapiLoopbackRecorder()
    except Exception as exc:
        pytest.skip(f"no WASAPI loopback device: {exc}")


def test_wav_matches_wall_clock_even_when_nothing_plays(recorder, tmp_path):
    path = tmp_path / "loopback.wav"
    recorder.start(path)
    time.sleep(2)
    recorder.stop()

    with wave.open(str(path)) as wav:
        seconds = wav.getnframes() / wav.getframerate()
    assert 1.8 < seconds < 2.2, f"expected ~2s of audio, got {seconds:.2f}s"


@pytest.fixture
def backend_cls(monkeypatch):
    from yefees_recorder import windows

    monkeypatch.setattr(windows, "list_monitors", lambda: [(0, 0, 1920, 1080), (1920, 0, 2560, 1440)])
    return windows.WindowsBackend


def test_display_resolves_to_that_monitors_offset_and_size(backend_cls, tmp_path):
    command = backend_cls(tmp_path / "o.mp4", display=1, audio=False).video_input_args()
    assert command[command.index("-offset_x") + 1] == "1920"
    assert command[command.index("-video_size") + 1] == "2560x1440"


def test_whole_desktop_when_nothing_is_selected(backend_cls, tmp_path):
    command = backend_cls(tmp_path / "o.mp4", audio=False).video_input_args()
    assert "-offset_x" not in command and command[-1] == "desktop"


def test_window_is_targeted_by_title_not_by_region(backend_cls, tmp_path):
    command = backend_cls(tmp_path / "o.mp4", window="Notepad", audio=False).video_input_args()
    assert command[-1] == "title=Notepad"
    assert "-video_size" not in command


def test_out_of_range_display_names_the_real_count(backend_cls, tmp_path):
    with pytest.raises(RuntimeError, match="this machine has 2"):
        backend_cls(tmp_path / "o.mp4", display=7, audio=False).video_input_args()

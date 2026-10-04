"""Windows loopback audio. The silent-machine case is the one that used to
break: WASAPI stops delivering when nothing plays, so the wav has to be padded
up to wall clock or it drifts out of sync with the video."""

import platform
import time
import wave

import pytest

pytestmark = pytest.mark.skipif(platform.system() != "Windows", reason="Windows only")


@pytest.fixture(params=[True, False], ids=["loopback", "mic"])
def recorder(request):
    from yefees_recorder.windows import WasapiRecorder

    try:
        return WasapiRecorder(loopback=request.param)
    except Exception as exc:
        pytest.skip(f"no WASAPI device: {exc}")


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
    """A two-monitor machine where Desktop Duplication is unavailable."""
    from yefees_recorder import windows

    monkeypatch.setattr(windows, "list_monitors", lambda: [(0, 0, 1920, 1080), (1920, 0, 2560, 1440)])
    monkeypatch.setattr(windows, "list_dxgi_outputs", lambda: [])
    return windows.WindowsBackend


@pytest.fixture
def gpu(monkeypatch):
    """The same two monitors, with DXGI numbering them the other way round.

    Measured on a real two-monitor machine: output 0 was \\\\.\\DISPLAY2, so an
    output index and a monitor index are not interchangeable.
    """
    from yefees_recorder import windows

    monkeypatch.setattr(windows, "list_monitors", lambda: [(0, 0, 1920, 1080), (1920, 0, 2560, 1440)])
    monkeypatch.setattr(windows, "list_dxgi_outputs", lambda: [(1920, 0, 2560, 1440), (0, 0, 1920, 1080)])
    machine = {"nvenc": True, "probes": []}

    def can(source, *codec):
        machine["probes"].append((source, codec))
        return machine["nvenc"] or not codec

    monkeypatch.setattr(windows, "ffmpeg_can", can)
    return machine


def test_a_monitor_is_grabbed_from_the_output_at_its_position(gpu, tmp_path):
    from yefees_recorder.windows import WindowsBackend

    command = WindowsBackend(tmp_path / "o.mp4", display=1, fps=60, audio=False).capture_command(tmp_path / "s.mkv")
    source = command[command.index("lavfi") + 2]
    assert source.startswith("ddagrab=framerate=60:output_idx=0:")
    assert "video_size=2560x1440" in source


def test_nvenc_takes_the_gpu_frame_untouched(gpu, tmp_path):
    from yefees_recorder.windows import NVENC_CQ, WindowsBackend

    command = WindowsBackend(tmp_path / "o.mp4", display=0, audio=False).capture_command(tmp_path / "s.mkv")
    assert command[command.index("-c:v") + 1] == "h264_nvenc"
    # A software filter or pixel format conversion cannot touch a D3D11 frame.
    assert command[command.index("-vf") + 1] == "null"
    assert "-pix_fmt" not in command and "hwdownload" not in " ".join(command)
    assert NVENC_CQ["low"] > NVENC_CQ["balanced"] > NVENC_CQ["high"]


def test_without_nvenc_the_frame_is_downloaded_for_x264(gpu, tmp_path):
    from yefees_recorder.windows import WindowsBackend

    gpu["nvenc"] = False
    command = WindowsBackend(tmp_path / "o.mp4", display=0, audio=False).capture_command(tmp_path / "s.mkv")
    assert command[command.index("lavfi") + 2].endswith(",hwdownload,format=bgra")
    assert command[command.index("-c:v") + 1] == "libx264"


def test_the_capture_path_is_probed_once_not_per_segment(gpu, tmp_path):
    from yefees_recorder.windows import WindowsBackend

    backend = WindowsBackend(tmp_path / "o.mp4", display=0, audio=False)
    backend.capture_command(tmp_path / "s0.mkv")
    backend.capture_command(tmp_path / "s1.mkv")  # a resume after pause
    assert len(gpu["probes"]) == 1


def test_an_odd_region_is_trimmed_to_even(gpu, tmp_path):
    from yefees_recorder.windows import WindowsBackend

    args = WindowsBackend(tmp_path / "o.mp4", region=(1930, 10, 801, 601), audio=False).video_input_args()
    assert args[-1].endswith(":output_idx=0:offset_x=10:offset_y=10:video_size=800x600")


def test_a_region_across_monitors_falls_back_to_gdigrab(gpu, tmp_path):
    from yefees_recorder.windows import WindowsBackend

    args = WindowsBackend(tmp_path / "o.mp4", region=(1800, 0, 400, 300), audio=False).video_input_args()
    assert "gdigrab" in args


def test_every_monitor_at_once_falls_back_with_a_warning(gpu, tmp_path):
    from yefees_recorder.windows import WindowsBackend

    backend = WindowsBackend(tmp_path / "o.mp4", audio=False)
    assert backend.video_input_args()[-1] == "desktop"
    assert "--display" in backend.video_warning


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

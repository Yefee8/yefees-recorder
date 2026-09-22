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

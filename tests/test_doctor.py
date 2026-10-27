from typer.testing import CliRunner

from yefees_recorder.cli import app, install_hint, required_tools

runner = CliRunner()


def test_install_hint_per_platform():
    assert install_hint("ffmpeg", "Darwin") == "brew install ffmpeg"
    assert install_hint("ffmpeg", "Windows") == "winget install Gyan.FFmpeg"
    assert "package manager" in install_hint("ffmpeg", "Haiku")


def test_doctor_fails_when_tool_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda tool: None)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "missing" in result.stdout


def test_doctor_passes_when_tools_present(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")
    # On a real Mac doctor also checks Screen Recording, and a machine running
    # the tests has no reason to have granted it.
    monkeypatch.setattr("yefees_recorder.macos.screen_recording_permitted", lambda: True)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0


def test_doctor_fails_when_screen_recording_is_denied(monkeypatch):
    """Without the grant avfoundation hands ffmpeg no frames at all, measured on
    macOS 14 - not the black frames the docs assumed. doctor must not pass it."""
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr("yefees_recorder.macos.screen_recording_permitted", lambda: False)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "Screen Recording" in result.stdout


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip()


def test_wayland_also_needs_wf_recorder(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert required_tools() == ("ffmpeg", "wf-recorder")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert required_tools() == ("ffmpeg",)


def test_record_reports_backend_errors_without_a_traceback(monkeypatch, tmp_path):
    """The Wayland/permission help text is the whole point of those errors."""
    class Failing:
        def start(self):
            raise RuntimeError("wf-recorder is not installed")

    # Not the developer's own config: its output_dir would be checked first.
    monkeypatch.setenv("YEFEES_RECORDER_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr("yefees_recorder.cli.get_backend", lambda *a, **k: Failing())
    result = runner.invoke(app, ["record", "-d", "1", "-o", str(tmp_path / "out.mp4")])
    assert result.exit_code == 1
    assert "wf-recorder" in result.stdout
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_linux_install_hint_follows_the_package_manager(monkeypatch):
    present = {"pacman"}
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}" if tool in present else None)
    assert install_hint("wf-recorder", "Linux") == "sudo pacman -S wf-recorder"
    present = {"apt"}
    assert install_hint("ffmpeg", "Linux") == "sudo apt install ffmpeg"


def test_an_unwritable_output_folder_is_refused_before_recording(monkeypatch, tmp_path):
    """Found on Arch: output_dir = "/ders" works on Windows (C:\\ders) and cannot
    be created at / on Linux. Finding out at the end lost the whole recording."""
    started = []
    monkeypatch.setenv("YEFEES_RECORDER_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr("yefees_recorder.cli.get_backend", lambda *a, **k: started.append(1))
    blocked = tmp_path / "file"
    blocked.write_text("")  # a file where the folder would have to go
    result = runner.invoke(app, ["record", "-d", "1", "-o", str(blocked / "sub" / "out.mp4")])
    assert result.exit_code == 1
    assert "Cannot write to" in result.stdout
    assert not started

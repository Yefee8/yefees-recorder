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
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0


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


def test_record_reports_backend_errors_without_a_traceback(monkeypatch):
    """The Wayland/permission help text is the whole point of those errors."""
    class Failing:
        def start(self):
            raise RuntimeError("wf-recorder is not installed")

    monkeypatch.setattr("yefees_recorder.cli.get_backend", lambda *a, **k: Failing())
    result = runner.invoke(app, ["record", "-d", "1"])
    assert result.exit_code == 1
    assert "wf-recorder" in result.stdout
    assert result.exception is None or isinstance(result.exception, SystemExit)

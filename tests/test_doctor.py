from typer.testing import CliRunner

from yefees_recorder.cli import app, install_hint

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

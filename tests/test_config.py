"""Config file loading and the flag > file > default precedence."""

import pytest
from typer.testing import CliRunner

from yefees_recorder import config
from yefees_recorder.cli import app

runner = CliRunner()


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setenv(config.ENV_VAR, str(path))
    return path


def test_missing_file_is_not_an_error(config_file):
    assert config.load() == ({}, {}, [])


def test_reads_known_settings(config_file):
    config_file.write_text('fps = 60\nquality = "high"\n', encoding="utf-8")
    values, _, warnings = config.load()
    assert values == {"fps": 60, "quality": "high"}
    assert warnings == []


def test_unknown_and_mistyped_settings_are_dropped_with_a_warning(config_file):
    config_file.write_text('fps = "sixty"\nnope = 1\nquality = "high"\n', encoding="utf-8")
    values, _, warnings = config.load()
    assert values == {"quality": "high"}, "bad values must not reach the backend"
    assert len(warnings) == 2


def test_broken_toml_is_reported_rather_than_raised(config_file):
    config_file.write_text("this is not = = toml", encoding="utf-8")
    values, _, warnings = config.load()
    assert values == {}
    assert warnings and "Ignoring" in warnings[0]


def test_booleans_are_not_accepted_as_numbers(config_file):
    """bool subclasses int in Python, so `display = true` would slip through."""
    config_file.write_text("display = true\naudio = 1\n", encoding="utf-8")
    values, _, warnings = config.load()
    assert values == {}
    assert len(warnings) == 2


@pytest.mark.parametrize(
    "flag, configured, expected",
    [(15, {"fps": 60}, 15), (None, {"fps": 60}, 60), (None, {}, 30)],
)
def test_flag_beats_file_beats_default(flag, configured, expected):
    assert config.resolve("fps", flag, configured) == expected


def test_audio_false_in_the_file_survives_resolution():
    """`audio = false` must not be mistaken for 'unset' and flipped back on."""
    assert config.resolve("audio", None, {"audio": False}) is False


@pytest.fixture
def fake_backend(monkeypatch):
    class Fake:
        audio_error = None

        def __init__(self, output, **kwargs):
            self.kwargs = kwargs
            self.output = output

        def start(self):
            pass

        def stop(self):
            return self.output

    made = {}

    def build(output, **kwargs):
        made["backend"] = Fake(output, **kwargs)
        return made["backend"]

    monkeypatch.setattr("yefees_recorder.cli.get_backend", build)
    return made


def test_record_takes_its_defaults_from_the_config_file(config_file, fake_backend, tmp_path):
    out = tmp_path / "out"
    config_file.write_text(
        f'fps = 12\nquality = "low"\naudio = false\ndisplay = 1\noutput_dir = "{out.as_posix()}"\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["record", "-d", "0.1"])
    assert result.exit_code == 0, result.stdout
    backend = fake_backend["backend"]
    assert backend.kwargs["fps"] == 12
    assert backend.kwargs["quality"] == "low"
    assert backend.kwargs["audio"] is False
    assert backend.kwargs["display"] == 1
    assert backend.output.parent == out


def test_flags_still_win_over_the_config_file(config_file, fake_backend):
    config_file.write_text('fps = 12\ndisplay = 1\n', encoding="utf-8")
    result = runner.invoke(app, ["record", "-d", "0.1", "--fps", "50", "--region", "0,0,10x10"])
    assert result.exit_code == 0, result.stdout
    backend = fake_backend["backend"]
    assert backend.kwargs["fps"] == 50
    assert backend.kwargs["region"] == (0, 0, 10, 10)
    assert backend.kwargs["display"] is None, "a region must not inherit display from config"


def test_pick_refuses_to_fight_an_explicit_source(config_file, fake_backend):
    result = runner.invoke(app, ["record", "--pick", "--display", "0"])
    assert result.exit_code == 1
    assert "do not also pass one" in result.stdout


def test_config_command_reports_where_each_value_came_from(config_file):
    config_file.write_text("fps = 99\n", encoding="utf-8")
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "99" in result.stdout and "config file" in result.stdout


def test_config_init_writes_a_template_and_never_clobbers(config_file):
    assert runner.invoke(app, ["config", "--init"]).exit_code == 0
    assert config_file.exists()
    config_file.write_text("fps = 7\n", encoding="utf-8")
    runner.invoke(app, ["config", "--init"])
    assert config_file.read_text(encoding="utf-8") == "fps = 7\n", "must not overwrite"


def test_the_written_template_is_valid_toml_that_changes_nothing(config_file):
    """It is a hand-written string, so it has to be parsed back to be trusted."""
    config.write_template()
    values, _, warnings = config.load()
    assert warnings == []
    assert values == {}, "every setting in the template should be commented out"


def test_pick_needs_a_terminal(config_file, fake_backend):
    result = runner.invoke(app, ["record", "--pick"])
    assert result.exit_code == 1
    assert "interactive terminal" in result.stdout


@pytest.mark.parametrize(
    "picked, expected",
    [("desktop", (None, None)), ("display", (0, None)), ("window", (None, "Firefox"))],
)
def test_menu_maps_the_choice_to_a_display_or_a_window(monkeypatch, picked, expected):
    from yefees_recorder import cli
    from yefees_recorder.capture import Source

    sources = {
        "desktop": None,
        "display": Source("display", "0", "Display 0"),
        "window": Source("window", "Firefox", "Firefox"),
    }
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli, "list_sources", lambda: [
        sources["display"], sources["window"],
        Source("audio", "Speakers", "Speakers"),  # must not appear in the menu
    ])
    monkeypatch.setattr(cli.menu, "choose", lambda *a, **k: sources[picked])
    assert cli._pick_source() == expected

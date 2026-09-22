"""Writing settings back, and the interactive editor that drives it."""

import pytest
from rich.prompt import Confirm, IntPrompt, Prompt
from typer.testing import CliRunner

from yefees_recorder import cli, config, keys
from yefees_recorder.capture import Source
from yefees_recorder.cli import app

runner = CliRunner()


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setenv(config.ENV_VAR, str(path))
    return path


class TestSetValues:
    def test_uncomments_a_template_line_instead_of_duplicating_it(self, config_file):
        config.write_template(config_file)
        config.set_values({"fps": 24})
        text = config_file.read_text(encoding="utf-8")
        assert "fps = 24" in text
        assert "# fps = 30" not in text
        assert config.load().values == {"fps": 24}

    def test_new_settings_land_above_presets_not_inside_them(self, config_file):
        """Appending at the end would silently make the setting part of a preset."""
        config_file.write_text(
            'fps = 30\n\n[presets.clip]\nquality = "low"\n', encoding="utf-8"
        )
        config.set_values({"mic": True})
        loaded = config.load()
        assert loaded.values == {"fps": 30, "mic": True}
        assert loaded.presets == {"clip": {"quality": "low"}}, "the preset must be untouched"

    def test_rewrites_in_place_rather_than_adding_a_second_line(self, config_file):
        config_file.write_text("fps = 30\n", encoding="utf-8")
        config.set_values({"fps": 60})
        assert config_file.read_text(encoding="utf-8").count("fps =") == 1
        assert config.load().values == {"fps": 60}

    def test_none_removes_a_setting(self, config_file):
        config_file.write_text('fps = 30\nquality = "low"\n', encoding="utf-8")
        config.set_values({"fps": None})
        assert config.load().values == {"quality": "low"}

    def test_keeps_hand_written_comments(self, config_file):
        config_file.write_text("# keep me\nfps = 30\n", encoding="utf-8")
        config.set_values({"fps": 60, "mic": True})
        assert "# keep me" in config_file.read_text(encoding="utf-8")

    def test_refuses_an_unknown_setting(self, config_file):
        with pytest.raises(ValueError, match="Unknown setting"):
            config.set_values({"nonsense": 1})

    def test_values_survive_a_round_trip_through_the_parser(self, config_file):
        values = {"fps": 60, "audio": False, "mic": True, "audio_offset": 0.25,
                  "quality": "high", "window": 'a "quoted" name'}
        config.set_values(values)
        loaded = config.load()
        assert loaded.values == values
        assert loaded.warnings == []


class Answers:
    """Stands in for the rich prompts, replaying what a user would type."""

    def __init__(self, choices=(), texts=(), confirms=(), numbers=()):
        self.choices, self.texts = iter(choices), iter(texts)
        self.confirms, self.numbers = iter(confirms), iter(numbers)

    def install(self, monkeypatch):
        monkeypatch.setattr(Prompt, "ask", classmethod(
            lambda cls, message, **kw: next(
                self.choices if "Choose" in str(message) else self.texts
            )
        ))
        monkeypatch.setattr(Confirm, "ask", classmethod(lambda cls, *a, **kw: next(self.confirms)))
        monkeypatch.setattr(IntPrompt, "ask", classmethod(lambda cls, *a, **kw: next(self.numbers)))


@pytest.fixture
def interactive(monkeypatch):
    monkeypatch.setattr(keys, "interactive", lambda: True)
    monkeypatch.setattr(cli, "list_sources", lambda: [
        Source("display", "0", "Display 0"),
        Source("display", "1", "Display 1"),
        Source("audio", "Speakers", "system audio"),
        Source("mic", "Headset Mic", "microphone"),
    ])


class TestEditor:
    def test_editing_needs_a_terminal(self, config_file, monkeypatch):
        monkeypatch.setattr(keys, "interactive", lambda: False)
        result = runner.invoke(app, ["config", "--edit"])
        assert result.exit_code == 1
        assert "interactive terminal" in result.stdout

    def test_a_session_writes_only_what_changed(self, config_file, interactive, monkeypatch):
        # 2 = fps -> 24, 6 = mic -> yes, then save
        Answers(choices=["2", "6", "s"], texts=["24"], confirms=[True]).install(monkeypatch)
        cli._edit_config()
        assert config.load().values == {"fps": 24, "mic": True}

    def test_device_settings_offer_the_real_devices(self, config_file, interactive, monkeypatch):
        # 7 = mic_device, picking entry 1 from the listed microphones
        Answers(choices=["7", "s"], numbers=[1]).install(monkeypatch)
        cli._edit_config()
        assert config.load().values == {"mic_device": "Headset Mic"}

    def test_display_is_stored_as_a_number_not_a_string(self, config_file, interactive, monkeypatch):
        Answers(choices=["9", "s"], numbers=[2]).install(monkeypatch)
        cli._edit_config()
        assert config.load().values == {"display": 1}
        assert config.load().warnings == []

    def test_choosing_automatic_clears_a_setting(self, config_file, interactive, monkeypatch):
        config_file.write_text("display = 1\n", encoding="utf-8")
        Answers(choices=["9", "s"], numbers=[0]).install(monkeypatch)
        cli._edit_config()
        assert config.load().values == {}

    def test_quitting_writes_nothing(self, config_file, interactive, monkeypatch):
        Answers(choices=["2", "q"], texts=["24"], confirms=[True]).install(monkeypatch)
        cli._edit_config()
        assert not config_file.exists() or config.load().values == {}

    def test_a_rejected_value_leaves_the_setting_alone(self, config_file, interactive, monkeypatch):
        # "abc" is not a frame rate, so nothing should be staged
        Answers(choices=["2", "s"], texts=["abc"]).install(monkeypatch)
        cli._edit_config()
        assert config.load().values == {}

    def test_nonsense_menu_input_does_not_crash(self, config_file, interactive, monkeypatch):
        Answers(choices=["99", "zz", "s"]).install(monkeypatch)
        cli._edit_config()
        assert config.load().values == {}

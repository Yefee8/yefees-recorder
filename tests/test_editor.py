"""Writing settings back, the arrow-key menu, and the editor built on it."""

import pytest
from rich.console import Console

from yefees_recorder import config, editor, keys, menu
from yefees_recorder.capture import Source
from yefees_recorder.menu import CANCELLED, Item

console = Console(force_terminal=False, width=100)


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
        config_file.write_text('fps = 30\n\n[presets.clip]\nquality = "low"\n', encoding="utf-8")
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
        values = {"fps": 60, "audio": False, "mic": True, "mic_gain": 2.5,
                  "quality": "high", "window": 'a "quoted" name'}
        config.set_values(values)
        loaded = config.load()
        assert loaded.values == values
        assert loaded.warnings == []


def press(monkeypatch, *presses):
    """Feed keystrokes to the menu, ending the loop rather than hanging."""
    remaining = iter(presses)
    monkeypatch.setattr(keys, "interactive", lambda: True)
    monkeypatch.setattr(keys, "read_key", lambda timeout: next(remaining, keys.ESC))


class TestMenu:
    def test_enter_returns_the_highlighted_value(self, monkeypatch):
        press(monkeypatch, keys.DOWN, keys.ENTER)
        assert menu.choose("t", [Item("a", "a"), Item("b", "b")], console=console) == "b"

    def test_moving_up_from_the_top_wraps_to_the_bottom(self, monkeypatch):
        press(monkeypatch, keys.UP, keys.ENTER)
        assert menu.choose("t", [Item("a", "a"), Item("b", "b")], console=console) == "b"

    def test_disabled_items_are_skipped(self, monkeypatch):
        items = [Item("a", "a"), Item("off", "off", enabled=False), Item("c", "c")]
        press(monkeypatch, keys.DOWN, keys.ENTER)
        assert menu.choose("t", items, console=console) == "c"

    def test_escape_cancels(self, monkeypatch):
        press(monkeypatch, keys.ESC)
        assert menu.choose("t", [Item("a", "a")], console=console) is CANCELLED

    def test_left_goes_back_when_allowed(self, monkeypatch):
        press(monkeypatch, keys.LEFT)
        assert menu.choose("t", [Item("a", "a")], console=console) is CANCELLED

    def test_left_is_ignored_at_the_top_level(self, monkeypatch):
        press(monkeypatch, keys.LEFT, keys.ENTER)
        assert menu.choose("t", [Item("a", "a")], console=console, on_left=False) == "a"

    def test_text_field_accepts_typing_and_backspace(self, monkeypatch):
        press(monkeypatch, "6", "0", "9", keys.BACKSPACE, keys.ENTER)
        assert menu.ask_text("n", console=console) == "60"

    def test_text_field_rejects_a_bad_value_and_stays_open(self, monkeypatch):
        press(monkeypatch, "x", keys.ENTER, keys.BACKSPACE, "7", keys.ENTER)
        assert menu.ask_text("n", console=console,
                             validate=lambda t: None if t.isdigit() else "nope") == "7"


class Script:
    """Stands in for the menus, replaying what the user would pick."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.titles = []

    def install(self, monkeypatch):
        def choose(screen, title, items, **kwargs):
            self.titles.append(title)
            return self.answers.pop(0)

        def ask_text(screen, prompt, **kwargs):
            self.titles.append(prompt)
            return self.answers.pop(0)

        def slider(screen, title, **kwargs):
            self.titles.append(title)
            return self.answers.pop(0)

        monkeypatch.setattr(menu.Screen, "choose", choose)
        monkeypatch.setattr(menu.Screen, "ask_text", ask_text)
        monkeypatch.setattr(menu.Screen, "slider", slider)


@pytest.fixture
def interactive(monkeypatch):
    monkeypatch.setattr(keys, "interactive", lambda: True)
    monkeypatch.setattr(editor, "list_sources", lambda: [
        Source("display", "0", "Display 0"),
        Source("display", "1", "Display 1"),
        Source("window", "Firefox", "Firefox"),
        Source("audio", "Speakers", "system audio"),
        Source("mic", "Headset Mic", "microphone"),
    ])


class TestEditor:
    def test_editing_needs_a_terminal(self, config_file, monkeypatch, capsys):
        monkeypatch.setattr(keys, "interactive", lambda: False)
        assert editor.run(console) is False

    def test_choosing_a_window_clears_the_monitor_and_the_area(
        self, config_file, interactive, monkeypatch
    ):
        """The whole point of one shared menu: these three cannot coexist."""
        config_file.write_text('display = 1\n', encoding="utf-8")
        Script("video", "window", "Firefox", "save").install(monkeypatch)
        assert editor.run(console) is True
        saved = config.load().values
        assert saved["window"] == "Firefox"
        assert "display" not in saved and "region" not in saved

    def test_choosing_a_monitor_clears_a_window(self, config_file, interactive, monkeypatch):
        config_file.write_text('window = "Firefox"\n', encoding="utf-8")
        Script("video", "display", "1", "save").install(monkeypatch)
        assert editor.run(console) is True
        saved = config.load().values
        assert saved["display"] == 1, "a monitor must be stored as a number"
        assert "window" not in saved

    def test_whole_desktop_clears_everything(self, config_file, interactive, monkeypatch):
        config_file.write_text('region = "0,0,800x600"\n', encoding="utf-8")
        Script("video", "all", "save").install(monkeypatch)
        assert editor.run(console) is True
        assert config.load().values == {}

    def test_an_area_replaces_a_monitor(self, config_file, interactive, monkeypatch):
        config_file.write_text("display = 0\n", encoding="utf-8")
        Script("video", "region", "10,20,640x480", "save").install(monkeypatch)
        assert editor.run(console) is True
        assert config.load().values == {"region": "10,20,640x480"}

    def test_audio_toggles_and_device_choice(self, config_file, interactive, monkeypatch):
        Script("audio", "mic", "mic_device", "Headset Mic", CANCELLED, "save").install(monkeypatch)
        assert editor.run(console) is True
        saved = config.load().values
        assert saved["mic"] is True
        assert saved["mic_device"] == "Headset Mic"

    def test_gain_is_chosen_in_decibels_and_stored_as_a_multiplier(
        self, config_file, interactive, monkeypatch
    ):
        """The slider works in dB; ffmpeg's volume filter wants the multiplier."""
        Script("audio", "mic_gain", 6.0, CANCELLED, "save").install(monkeypatch)
        assert editor.run(console) is True
        saved = config.load()
        assert saved.values["mic_gain"] == pytest.approx(2.0, abs=0.01)
        assert saved.warnings == []

    def test_a_cancelled_gain_slider_changes_nothing(
        self, config_file, interactive, monkeypatch
    ):
        Script("audio", "mic_gain", CANCELLED, CANCELLED, "save").install(monkeypatch)
        assert editor.run(console) is False

    def test_quitting_writes_nothing(self, config_file, interactive, monkeypatch):
        Script("video", "display", "1", "quit", True).install(monkeypatch)
        assert editor.run(console) is False
        assert not config_file.exists() or config.load().values == {}

    def test_saving_with_no_changes_writes_nothing(self, config_file, interactive, monkeypatch):
        Script("save").install(monkeypatch)
        assert editor.run(console) is False

    def test_backing_out_of_a_submenu_keeps_earlier_edits(
        self, config_file, interactive, monkeypatch
    ):
        Script("output", "fps", 48.0, CANCELLED, "save").install(monkeypatch)
        assert editor.run(console) is True
        assert config.load().values == {"fps": 48}

    def test_cancelling_a_device_choice_changes_nothing(
        self, config_file, interactive, monkeypatch
    ):
        Script("audio", "audio_device", CANCELLED, CANCELLED, "save").install(monkeypatch)
        assert editor.run(console) is False


class TestSlider:
    def make(self, monkeypatch, *presses, **options):
        press(monkeypatch, *presses)
        settings = {"value": 1.0, "minimum": 0.0, "maximum": 8.0, "step": 0.1, "coarse": 1.0}
        settings.update(options)
        with menu.Screen(console) as screen:
            return screen.slider("level", **settings)

    def test_right_and_left_move_by_one_step(self, monkeypatch):
        assert self.make(monkeypatch, keys.RIGHT, keys.RIGHT, keys.ENTER) == 1.2
        assert self.make(monkeypatch, keys.LEFT, keys.ENTER) == 0.9

    def test_up_and_down_move_by_the_coarse_step(self, monkeypatch):
        assert self.make(monkeypatch, keys.UP, keys.ENTER) == 2.0
        assert self.make(monkeypatch, keys.DOWN, keys.ENTER) == 0.0

    def test_repeated_steps_do_not_drift(self, monkeypatch):
        """Floating point would otherwise land on 1.9000000000000001."""
        value = self.make(monkeypatch, *([keys.RIGHT] * 9), keys.ENTER)
        assert value == 1.9

    def test_it_stops_at_the_ends(self, monkeypatch):
        assert self.make(monkeypatch, *([keys.UP] * 20), keys.ENTER) == 8.0
        assert self.make(monkeypatch, *([keys.DOWN] * 20), keys.ENTER) == 0.0

    def test_escape_keeps_the_old_value(self, monkeypatch):
        assert self.make(monkeypatch, keys.RIGHT, keys.ESC) is CANCELLED

    def test_a_starting_value_outside_the_range_is_clamped(self, monkeypatch):
        assert self.make(monkeypatch, keys.ENTER, value=99.0) == 8.0

    def test_decibels_are_reported_the_way_audio_people_read_them(self):
        assert menu.as_decibels(1.0) == "unchanged"
        assert menu.as_decibels(2.0) == "+6.0 dB"
        assert menu.as_decibels(0.5) == "-6.0 dB"
        assert menu.as_decibels(0.0) == "silent"


def test_the_whole_session_uses_one_screen(config_file, interactive, monkeypatch):
    """Each page must redraw in place, not print another menu underneath."""
    opened = []
    real_enter = menu.Screen.__enter__
    monkeypatch.setattr(menu.Screen, "__enter__",
                        lambda self: (opened.append(1), real_enter(self))[1])
    Script("video", "display", "1", "audio", "mic", CANCELLED, "output", CANCELLED,
           "save").install(monkeypatch)
    editor.run(console)
    assert opened == [1], f"opened {len(opened)} live regions instead of one"


class TestSliderTyping:
    def test_pressing_t_opens_a_field_for_an_exact_value(self, monkeypatch):
        press(monkeypatch, "t", "3", ".", "7", keys.ENTER, keys.ENTER)
        with menu.Screen(console) as screen:
            value = screen.slider("level", value=0.0, minimum=-40.0, maximum=24.0,
                                  step=0.5, coarse=3.0)
        assert value == 3.7, "a typed value the steps cannot land on must survive"

    def test_a_typed_value_outside_the_range_is_refused(self, monkeypatch):
        press(monkeypatch, "t", "9", "9", keys.ENTER,
              keys.BACKSPACE, keys.BACKSPACE, "5", keys.ENTER, keys.ENTER)
        with menu.Screen(console) as screen:
            value = screen.slider("level", value=0.0, minimum=-40.0, maximum=24.0,
                                  step=0.5, coarse=3.0)
        assert value == 5.0

    def test_typing_can_be_turned_off(self, monkeypatch):
        press(monkeypatch, "t", keys.RIGHT, keys.ENTER)
        with menu.Screen(console) as screen:
            value = screen.slider("level", value=0.0, minimum=0.0, maximum=10.0,
                                  step=1.0, allow_typing=False)
        assert value == 1.0, "t should have been ignored, not opened a field"

    def test_decibels_round_trip_through_the_multiplier(self):
        for decibels in (-40.0, -6.0, 0.0, 6.0, 24.0):
            gain = menu.db_to_gain(decibels)
            assert menu.gain_to_db(gain) == pytest.approx(decibels, abs=0.01)


class TestSourceScanning:
    def test_devices_are_enumerated_once_per_session(self, config_file, monkeypatch):
        """Scanning costs ~0.5s on Windows; doing it per page is the freeze."""
        calls = []
        monkeypatch.setattr(keys, "interactive", lambda: True)
        monkeypatch.setattr(editor, "list_sources", lambda: calls.append(1) or [
            Source("display", "0", "Display 0"),
            Source("mic", "Headset Mic", "microphone"),
        ])
        # visit audio, pick a device, visit video, pick a monitor, then save
        Script("audio", "mic_device", "Headset Mic", CANCELLED,
               "video", "display", "0", "save").install(monkeypatch)
        editor.run(console)
        assert len(calls) == 1, f"scanned {len(calls)} times instead of once"

    def test_rescan_looks_again(self, monkeypatch):
        calls = []
        monkeypatch.setattr(keys, "interactive", lambda: True)
        monkeypatch.setattr(editor, "list_sources", lambda: calls.append(1) or [
            Source("mic", "Headset Mic", "microphone"),
        ])
        screen = menu.Screen(console)
        sources = editor.Sources()
        answers = iter([editor.RESCAN, "Headset Mic"])
        monkeypatch.setattr(menu.Screen, "choose",
                            lambda self, *a, **k: next(answers))
        picked = editor._pick_device(screen, sources, "mic", "Microphone", None)
        assert picked == "Headset Mic"
        assert len(calls) == 2, "rescan must actually look again"

"""The interactive settings editor behind `config --edit`.

The whole session shares one `menu.Screen`, so moving between pages replaces
what is on screen rather than leaving a trail of menus behind.

Each page is split the same way as the menus themselves: a `_*_items` function
says what the page looks like, and an `_edit_*` function says what a choice
does. Keeping those apart is what stops the pages turning into one long
if/elif ladder.

The video source is a single choice: picking a window clears the monitor and
the area, because only one of them can be recorded. Nothing touches the config
file until the user saves, and saving writes only what actually changed.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console

from . import config as user_config
from . import keys, menu
from .capture import list_sources, parse_region
from .menu import CANCELLED, Item, Screen

SOURCE_KEYS = ("display", "window", "region")

# Levels are multipliers. 8x is +18 dB, past which anything useful has clipped.
GAIN_RANGE = {"minimum": 0.0, "maximum": 8.0, "step": 0.1, "coarse": 1.0}
OFFSET_RANGE = {"minimum": -2.0, "maximum": 2.0, "step": 0.05, "coarse": 0.25}
FPS_RANGE = {"minimum": 5, "maximum": 120, "step": 1, "coarse": 10}

DEVICE_PAGES = {
    "audio_device": ("audio", "System audio source"),
    "mic_device": ("mic", "Microphone"),
    "app_audio": ("app", "Application"),
}


# ----------------------------------------------------------------- labelling
def _sources_of(kind: str) -> list:
    try:
        return [source for source in list_sources() if source.kind == kind]
    except (NotImplementedError, OSError, RuntimeError):
        return []


def _setting(values: dict, key: str) -> Any:
    return values.get(key, user_config.DEFAULTS[key])


def _on_off(enabled: Any) -> str:
    return "on" if enabled else "off"


def _or_auto(value: Any) -> str:
    return "automatic" if value in (None, "") else str(value)


def _gain_label(gain: float) -> str:
    return f"x{gain:g}  ({menu.as_decibels(gain)})"


def _gain_tone(gain: float) -> str:
    """Warn once a gain is high enough to start clipping."""
    if gain > 4:
        return "loud"
    return "info" if gain != 1.0 else "muted"


def _offset_label(seconds: float) -> str:
    if abs(seconds) < 1e-9:
        return "in step"
    return "audio later" if seconds > 0 else "audio earlier"


def _describe_source(values: dict) -> str:
    if values.get("window"):
        return f"window: {values['window']}"
    if values.get("region"):
        return f"area: {values['region']}"
    if values.get("display") is not None:
        return f"monitor {values['display']}"
    return "whole desktop"


def _describe_audio(values: dict) -> str:
    parts = [
        f"system {_on_off(_setting(values, 'audio'))}",
        f"mic {_on_off(_setting(values, 'mic'))}",
    ]
    if values.get("app_audio"):
        parts.append(f"app: {values['app_audio']}")
    return ", ".join(parts)


def _describe_output(values: dict) -> str:
    return (
        f"{_or_auto(values.get('output_dir'))}, "
        f"{_setting(values, 'fps')} fps, {_setting(values, 'quality')}"
    )


# ------------------------------------------------------------- small helpers
def _store(values: dict, key: str, picked: Any, convert=lambda value: value) -> None:
    """Keep a chosen value unless the user backed out of the page."""
    if picked is not CANCELLED:
        values[key] = convert(picked)


def _pick_device(screen: Screen, kind: str, title: str, current: Any) -> Any:
    """Choose from the devices this machine actually has, or CANCELLED."""
    found = _sources_of(kind)
    if not found:
        return screen.ask_text(f"{title} (none detected, type a name)", current=str(current or ""))

    items = [Item("Automatic", None, "let the recorder choose")]
    items += [
        Item(source.id, source.id,
             source.name if source.name != source.id else "", tone="info")
        for source in found
    ]
    cursor = next((n for n, item in enumerate(items) if item.value == current), 0)
    return screen.choose(title, items, cursor=cursor)


def _pick_gain(screen: Screen, label: str, current: float) -> Any:
    return screen.slider(
        f"{label}   (1.0 leaves it alone; microphones usually need more)",
        value=float(current), describe=menu.as_decibels, **GAIN_RANGE,
    )


# ------------------------------------------------------------- video source
def _video_items(values: dict) -> list[Item]:
    return [
        Item("Whole desktop", "all", "every monitor side by side"),
        Item("A monitor", "display", "record one screen"),
        Item("A window", "window", "record a single window"),
        Item("An area", "region", "record a rectangle"),
    ]


def _choose_video_source(screen: Screen, values: dict, chosen: str) -> bool:
    """Apply one video-source choice. Returns True once something was set.

    Every branch clears all three keys first: they are mutually exclusive, so
    setting one has to unset the others.
    """
    picked: Any = None
    if chosen == "display":
        picked = _pick_device(screen, "display", "Which monitor", values.get("display"))
    elif chosen == "window":
        picked = _pick_device(screen, "window", "Which window", values.get("window"))
    elif chosen == "region":
        picked = screen.ask_text("Area as x,y,WIDTHxHEIGHT",
                                 current=values.get("region") or "", validate=_check_region)
    if picked is CANCELLED:
        return False

    values.update(dict.fromkeys(SOURCE_KEYS))
    if chosen == "display" and picked is not None:
        values["display"] = int(picked)
    elif chosen in ("window", "region"):
        values[chosen] = picked or None
    return True


def _check_region(text: str) -> str | None:
    try:
        parse_region(text)
    except ValueError as exc:
        return str(exc)
    return None


def _video_menu(screen: Screen, values: dict) -> None:
    while True:
        chosen = screen.choose(
            f"Video source  -  currently {_describe_source(values)}", _video_items(values),
            hint=menu.HINT_MENU + "   (picking one replaces the others)",
        )
        if chosen is CANCELLED:
            return
        if _choose_video_source(screen, values, chosen):
            return


# --------------------------------------------------------------------- audio
def _audio_items(values: dict, app_available: bool) -> list[Item]:
    system_on = _setting(values, "audio")
    mic_on = _setting(values, "mic")
    offset = _setting(values, "audio_offset")
    audio_gain = _setting(values, "audio_gain")
    mic_gain = _setting(values, "mic_gain")
    return [
        Item("Record system audio", "audio", _on_off(system_on),
             tone="good" if system_on else "muted"),
        Item("  System audio device", "audio_device", _or_auto(values.get("audio_device")),
             enabled=bool(system_on), reason="system audio is off", tone="info"),
        Item("  System audio level", "audio_gain", _gain_label(audio_gain),
             enabled=bool(system_on), reason="system audio is off", tone=_gain_tone(audio_gain)),
        Item("Record microphone", "mic", _on_off(mic_on), tone="good" if mic_on else "muted"),
        Item("  Microphone device", "mic_device", _or_auto(values.get("mic_device")),
             enabled=bool(mic_on), reason="microphone is off", tone="info"),
        Item("  Microphone level", "mic_gain", _gain_label(mic_gain),
             enabled=bool(mic_on), reason="microphone is off", tone=_gain_tone(mic_gain)),
        Item("Record one application", "app_audio", _or_auto(values.get("app_audio")),
             enabled=app_available, reason="not supported on this system - see --app-audio",
             tone="info"),
        Item("Audio offset", "audio_offset", f"{offset:g} s  ({_offset_label(offset)})",
             tone="muted" if offset == 0 else "warn"),
    ]


def _edit_audio(screen: Screen, values: dict, chosen: str) -> None:
    if chosen in ("audio", "mic"):
        values[chosen] = not _setting(values, chosen)
    elif chosen in DEVICE_PAGES:
        kind, title = DEVICE_PAGES[chosen]
        picked = _pick_device(screen, kind, title, values.get(chosen))
        _store(values, chosen, picked, convert=lambda name: name or None)
    elif chosen in ("audio_gain", "mic_gain"):
        label = "System audio level" if chosen == "audio_gain" else "Microphone level"
        _store(values, chosen, _pick_gain(screen, label, _setting(values, chosen)))
    elif chosen == "audio_offset":
        picked = screen.slider(
            "Audio offset   (nudge this if voices land out of step)",
            value=float(_setting(values, "audio_offset")),
            describe=_offset_label, **OFFSET_RANGE,
        )
        _store(values, "audio_offset", picked)


def _audio_menu(screen: Screen, values: dict) -> None:
    app_available = bool(_sources_of("app"))
    while True:
        chosen = screen.choose("Audio", _audio_items(values, app_available))
        if chosen is CANCELLED:
            return
        _edit_audio(screen, values, chosen)


# ------------------------------------------------------------ output/quality
QUALITY_CHOICES = (
    ("low", "smallest files"),
    ("balanced", "the default"),
    ("high", "best looking"),
)


def _output_items(values: dict) -> list[Item]:
    return [
        Item("Save recordings in", "output_dir", _or_auto(values.get("output_dir")), tone="info"),
        Item("Frame rate", "fps", f"{_setting(values, 'fps')} fps", tone="info"),
        Item("Quality", "quality", str(_setting(values, "quality")), tone="info"),
    ]


def _edit_output(screen: Screen, values: dict, chosen: str) -> None:
    if chosen == "output_dir":
        typed = screen.ask_text("Folder for recordings", current=values.get("output_dir") or "")
        _store(values, "output_dir", typed, convert=lambda folder: folder or None)
    elif chosen == "fps":
        picked = screen.slider("Frames per second",
                               value=float(_setting(values, "fps")), **FPS_RANGE)
        _store(values, "fps", picked, convert=int)
    elif chosen == "quality":
        items = [Item(name, name, hint, tone="info") for name, hint in QUALITY_CHOICES]
        cursor = next((n for n, item in enumerate(items)
                       if item.value == _setting(values, "quality")), 1)
        _store(values, "quality", screen.choose("Quality", items, cursor=cursor))


def _output_menu(screen: Screen, values: dict) -> None:
    while True:
        chosen = screen.choose("Output and quality", _output_items(values))
        if chosen is CANCELLED:
            return
        _edit_output(screen, values, chosen)


# --------------------------------------------------------------- the session
PAGES = {"video": _video_menu, "audio": _audio_menu, "output": _output_menu}


def _main_items(values: dict, original: dict) -> list[Item]:
    edited = values != original
    return [
        Item("Video source", "video", _describe_source(values), tone="info"),
        Item("Audio", "audio", _describe_audio(values), tone="info"),
        Item("Output and quality", "output", _describe_output(values), tone="info"),
        Item("Save and exit", "save",
             "write the changes" if edited else "no changes yet",
             tone="good" if edited else "muted"),
        Item("Quit without saving", "quit", tone="muted"),
    ]


def _changes_between(original: dict, values: dict) -> dict:
    return {
        key: values.get(key)
        for key in user_config.DEFAULTS
        if values.get(key) != original.get(key)
    }


def _confirm_discard(screen: Screen) -> bool:
    answer = screen.choose(
        "Discard your changes?",
        [Item("Keep editing", False, tone="good"), Item("Discard and quit", True, tone="warn")],
    )
    return answer is True


def _session(screen: Screen, path, original: dict) -> dict | None:
    """Run the menus. Returns the changes to write, or None to write nothing."""
    values = dict(original)
    while True:
        chosen = screen.choose(
            f"Settings  -  {path}", _main_items(values, original),
            hint=menu.HINT_MENU + "   (nothing is saved until you choose Save)",
            on_left=False,
        )
        if chosen in PAGES:
            PAGES[chosen](screen, values)
        elif chosen == "save":
            return _changes_between(original, values)
        elif values == original or _confirm_discard(screen):
            return None


def _report(console: Console, path, changes: dict) -> None:
    """Say what the file now holds, having read it back rather than trusting us."""
    written = user_config.load(path)
    console.print(f"[green]Saved to[/] {path}")
    for key in sorted(changes):
        console.print(f"  [cyan]{key}[/] = [bold]{written.values.get(key, '(default)')}[/]")
    for complaint in written.warnings:
        console.print(f"[yellow]{complaint}[/]")


def run(console: Console) -> bool:
    """Edit the settings. Returns True if anything was written."""
    if not keys.interactive():
        console.print("[red]Editing settings needs an interactive terminal.[/]")
        return False

    path = user_config.config_path()
    original = dict(user_config.load(path).values)

    with Screen(console) as screen:
        changes = _session(screen, path, original)

    if changes is None:
        console.print("Nothing written.")
        return False
    if not changes:
        console.print("No changes to save.")
        return False

    user_config.set_values(changes, path)
    _report(console, path, changes)
    return True

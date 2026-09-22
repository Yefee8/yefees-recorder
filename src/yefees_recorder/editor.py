"""The interactive settings editor behind `config --edit`.

The whole session shares one `menu.Screen`, so moving between pages replaces
what is on screen rather than leaving a trail of menus behind.

Settings are grouped into submenus, and the video source is a single choice:
picking a window clears the monitor and the area, because only one of them can
be recorded. Nothing touches the config file until the user saves, and saving
writes only what actually changed.
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


def _sources_of(kind: str):
    try:
        return [s for s in list_sources() if s.kind == kind]
    except (NotImplementedError, OSError, RuntimeError):
        return []


def _on_off(value: Any) -> str:
    return "on" if value else "off"


def _or_auto(value: Any) -> str:
    return "automatic" if value in (None, "") else str(value)


def _setting(values: dict, key: str) -> Any:
    return values.get(key, user_config.DEFAULTS[key])


def _gain_label(values: dict, key: str) -> str:
    gain = _setting(values, key)
    return f"x{gain:g}  ({menu.as_decibels(gain)})"


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


def _pick_device(screen: Screen, kind: str, title: str, current: Any):
    """Choose from the devices this machine actually has, or CANCELLED."""
    found = _sources_of(kind)
    if not found:
        return screen.ask_text(f"{title} (none detected, type a name)", current=str(current or ""))
    items = [Item("Automatic", None, "let the recorder choose")]
    items += [
        Item(source.id, source.id, source.name if source.name != source.id else "")
        for source in found
    ]
    cursor = next((n for n, i in enumerate(items) if i.value == current), 0)
    return screen.choose(title, items, cursor=cursor)


def _video_menu(screen: Screen, values: dict) -> None:
    """One choice: whole desktop, a monitor, a window, or an area."""
    while True:
        items = [
            Item("Whole desktop", "all", "every monitor side by side"),
            Item("A monitor", "display", "record one screen"),
            Item("A window", "window", "record a single window"),
            Item("An area", "region", "record a rectangle"),
        ]
        chosen = screen.choose(
            f"Video source  -  currently {_describe_source(values)}", items,
            hint=menu.HINT_MENU + "   (picking one replaces the others)",
        )
        if chosen is CANCELLED:
            return

        if chosen == "all":
            values.update(dict.fromkeys(SOURCE_KEYS))
            return

        if chosen == "display":
            picked = _pick_device(screen, "display", "Which monitor", values.get("display"))
            if picked is CANCELLED:
                continue
            values.update(dict.fromkeys(SOURCE_KEYS))
            values["display"] = int(picked) if picked is not None else None
            return

        if chosen == "window":
            picked = _pick_device(screen, "window", "Which window", values.get("window"))
            if picked is CANCELLED:
                continue
            values.update(dict.fromkeys(SOURCE_KEYS))
            values["window"] = picked or None
            return

        if chosen == "region":
            def check(text: str) -> str | None:
                try:
                    parse_region(text)
                except ValueError as exc:
                    return str(exc)
                return None

            typed = screen.ask_text("Area as x,y,WIDTHxHEIGHT",
                                    current=values.get("region") or "", validate=check)
            if typed is CANCELLED:
                continue
            values.update(dict.fromkeys(SOURCE_KEYS))
            values["region"] = typed or None
            return


def _audio_menu(screen: Screen, values: dict) -> None:
    app_sources = _sources_of("app")
    while True:
        system_on = _setting(values, "audio")
        mic_on = _setting(values, "mic")
        offset = _setting(values, "audio_offset")
        items = [
            Item("Record system audio", "audio", _on_off(system_on)),
            Item("  System audio device", "audio_device", _or_auto(values.get("audio_device")),
                 enabled=bool(system_on), reason="system audio is off"),
            Item("  System audio level", "audio_gain", _gain_label(values, "audio_gain"),
                 enabled=bool(system_on), reason="system audio is off"),
            Item("Record microphone", "mic", _on_off(mic_on)),
            Item("  Microphone device", "mic_device", _or_auto(values.get("mic_device")),
                 enabled=bool(mic_on), reason="microphone is off"),
            Item("  Microphone level", "mic_gain", _gain_label(values, "mic_gain"),
                 enabled=bool(mic_on), reason="microphone is off"),
            Item("Record one application", "app_audio", _or_auto(values.get("app_audio")),
                 enabled=bool(app_sources),
                 reason="not supported on this system - see --app-audio"),
            Item("Audio offset", "audio_offset", f"{offset:g} s  ({_offset_label(offset)})"),
        ]
        chosen = screen.choose("Audio", items)
        if chosen is CANCELLED:
            return

        if chosen in ("audio", "mic"):
            values[chosen] = not _setting(values, chosen)
        elif chosen in ("audio_device", "mic_device", "app_audio"):
            kind = {"audio_device": "audio", "mic_device": "mic", "app_audio": "app"}[chosen]
            title = {"audio_device": "System audio source", "mic_device": "Microphone",
                     "app_audio": "Application"}[chosen]
            picked = _pick_device(screen, kind, title, values.get(chosen))
            if picked is not CANCELLED:
                values[chosen] = picked or None
        elif chosen in ("audio_gain", "mic_gain"):
            label = "System audio level" if chosen == "audio_gain" else "Microphone level"
            picked = screen.slider(
                f"{label}   (1.0 leaves it alone; microphones usually need more)",
                value=float(_setting(values, chosen)), describe=menu.as_decibels, **GAIN_RANGE,
            )
            if picked is not CANCELLED:
                values[chosen] = picked
        elif chosen == "audio_offset":
            picked = screen.slider(
                "Audio offset   (nudge this if voices land out of step)",
                value=float(offset), describe=_offset_label, **OFFSET_RANGE,
            )
            if picked is not CANCELLED:
                values["audio_offset"] = picked


def _output_menu(screen: Screen, values: dict) -> None:
    while True:
        items = [
            Item("Save recordings in", "output_dir", _or_auto(values.get("output_dir"))),
            Item("Frame rate", "fps", f"{_setting(values, 'fps')} fps"),
            Item("Quality", "quality", str(_setting(values, "quality"))),
        ]
        chosen = screen.choose("Output and quality", items)
        if chosen is CANCELLED:
            return

        if chosen == "output_dir":
            typed = screen.ask_text("Folder for recordings", current=values.get("output_dir") or "")
            if typed is not CANCELLED:
                values["output_dir"] = typed or None
        elif chosen == "fps":
            picked = screen.slider("Frames per second",
                                   value=float(_setting(values, "fps")), **FPS_RANGE)
            if picked is not CANCELLED:
                values["fps"] = int(picked)
        elif chosen == "quality":
            options = [Item(name, name, hint) for name, hint in (
                ("low", "smallest files"), ("balanced", "the default"), ("high", "best looking"))]
            current = _setting(values, "quality")
            cursor = next((n for n, i in enumerate(options) if i.value == current), 1)
            picked = screen.choose("Quality", options, cursor=cursor)
            if picked is not CANCELLED:
                values["quality"] = picked


def _session(screen: Screen, path, original: dict) -> dict | None:
    """Run the menus. Returns the changes to write, or None to write nothing."""
    values = dict(original)
    while True:
        items = [
            Item("Video source", "video", _describe_source(values)),
            Item("Audio", "audio", _describe_audio(values)),
            Item("Output and quality", "output", _describe_output(values)),
            Item("Save and exit", "save",
                 "no changes yet" if values == original else "write the changes"),
            Item("Quit without saving", "quit"),
        ]
        chosen = screen.choose(
            f"Settings  -  {path}", items,
            hint=menu.HINT_MENU + "   (nothing is saved until you choose Save)",
            on_left=False,
        )

        if chosen == "video":
            _video_menu(screen, values)
        elif chosen == "audio":
            _audio_menu(screen, values)
        elif chosen == "output":
            _output_menu(screen, values)
        elif chosen in ("quit", CANCELLED):
            if values == original:
                return None
            confirm = screen.choose(
                "Discard your changes?",
                [Item("Keep editing", False), Item("Discard and quit", True)],
            )
            if confirm is True:
                return None
        elif chosen == "save":
            return {
                key: values.get(key)
                for key in user_config.DEFAULTS
                if values.get(key) != original.get(key)
            }


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
    # Read it back, so what is reported is what the file holds rather than what
    # we believe we wrote.
    written = user_config.load(path)
    console.print(f"[green]Saved to[/] {path}")
    for key in sorted(changes):
        console.print(f"  {key} = {written.values.get(key, '(default)')}")
    for complaint in written.warnings:
        console.print(f"[yellow]{complaint}[/]")
    return True

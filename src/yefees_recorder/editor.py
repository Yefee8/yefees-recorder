"""The interactive settings editor behind `config --edit`.

Settings are grouped into submenus rather than one flat list, and the video
source is a single choice: picking a window clears the monitor and the area,
because only one of them can be recorded.

Nothing touches the config file until the user saves, and saving writes only
what actually changed.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console

from . import config as user_config
from . import keys, menu
from .capture import list_sources, parse_region
from .menu import CANCELLED, Item

SOURCE_KEYS = ("display", "window", "region")


def _sources_of(kind: str):
    try:
        return [s for s in list_sources() if s.kind == kind]
    except (NotImplementedError, OSError, RuntimeError):
        return []


def _on_off(value: Any) -> str:
    return "on" if value else "off"


def _or_auto(value: Any) -> str:
    return "automatic" if value in (None, "") else str(value)


def _describe_source(values: dict) -> str:
    if values.get("window"):
        return f"window: {values['window']}"
    if values.get("region"):
        return f"area: {values['region']}"
    if values.get("display") is not None:
        return f"monitor {values['display']}"
    return "whole desktop"


def _describe_audio(values: dict) -> str:
    parts = [f"system {_on_off(values.get('audio', True))}", f"mic {_on_off(values.get('mic'))}"]
    if values.get("app_audio"):
        parts.append(f"app: {values['app_audio']}")
    return ", ".join(parts)


def _describe_output(values: dict) -> str:
    return (
        f"{_or_auto(values.get('output_dir'))}, "
        f"{values.get('fps', user_config.DEFAULTS['fps'])} fps, "
        f"{values.get('quality', user_config.DEFAULTS['quality'])}"
    )


def _pick_device(console: Console, kind: str, title: str, current: Any):
    """Choose from the devices this machine actually has. Returns CANCELLED or a value."""
    found = _sources_of(kind)
    if not found:
        return menu.ask_text(
            f"{title} (none detected, type a name)", console=console, current=str(current or "")
        )
    items = [Item("Automatic", None, "let the recorder choose")]
    items += [
        Item(source.id, source.id, source.name if source.name != source.id else "")
        for source in found
    ]
    cursor = next((n for n, i in enumerate(items) if i.value == current), 0)
    chosen = menu.choose(title, items, console=console, cursor=cursor)
    if chosen is CANCELLED:
        return CANCELLED
    return chosen


def _ask_number(console: Console, title: str, current: Any, *, whole: bool):
    def check(text: str) -> str | None:
        try:
            value = int(text) if whole else float(text)
        except ValueError:
            return "Enter a number." if not whole else "Enter a whole number."
        return "Must be greater than zero." if whole and value <= 0 else None

    answer = menu.ask_text(title, console=console, current="" if current is None else str(current),
                           validate=check)
    if answer is CANCELLED:
        return CANCELLED
    if not answer:
        return None
    return int(answer) if whole else float(answer)


def _video_menu(console: Console, values: dict) -> None:
    """One choice: whole desktop, a monitor, a window, or an area."""
    while True:
        active = _describe_source(values)
        items = [
            Item("Whole desktop", "all", "every monitor side by side"),
            Item("A monitor", "display", "record one screen"),
            Item("A window", "window", "record a single window"),
            Item("An area", "region", "record a rectangle"),
        ]
        chosen = menu.choose(
            f"Video source  -  currently {active}", items, console=console,
            hint=menu.HINT_MENU + "   (picking one replaces the others)",
        )
        if chosen is CANCELLED:
            return

        if chosen == "all":
            values.update(dict.fromkeys(SOURCE_KEYS))
            return

        if chosen == "display":
            picked = _pick_device(console, "display", "Which monitor", values.get("display"))
            if picked is CANCELLED:
                continue
            values.update(dict.fromkeys(SOURCE_KEYS))
            values["display"] = int(picked) if picked is not None else None
            return

        if chosen == "window":
            picked = _pick_device(console, "window", "Which window", values.get("window"))
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

            typed = menu.ask_text("Area as x,y,WIDTHxHEIGHT", console=console,
                                  current=values.get("region") or "", validate=check)
            if typed is CANCELLED:
                continue
            values.update(dict.fromkeys(SOURCE_KEYS))
            values["region"] = typed or None
            return


def _audio_menu(console: Console, values: dict) -> None:
    app_sources = _sources_of("app")
    while True:
        system_on = values.get("audio", user_config.DEFAULTS["audio"])
        mic_on = values.get("mic", user_config.DEFAULTS["mic"])
        items = [
            Item("Record system audio", "audio", _on_off(system_on)),
            Item("  System audio device", "audio_device", _or_auto(values.get("audio_device")),
                 enabled=bool(system_on), reason="system audio is off"),
            Item("  System audio level", "audio_gain",
                 f"x{values.get('audio_gain', 1.0)}", enabled=bool(system_on),
                 reason="system audio is off"),
            Item("Record microphone", "mic", _on_off(mic_on)),
            Item("  Microphone device", "mic_device", _or_auto(values.get("mic_device")),
                 enabled=bool(mic_on), reason="microphone is off"),
            Item("  Microphone level", "mic_gain", f"x{values.get('mic_gain', 1.0)}",
                 enabled=bool(mic_on), reason="microphone is off"),
            Item("Record one application", "app_audio", _or_auto(values.get("app_audio")),
                 enabled=bool(app_sources),
                 reason="not supported on this system - see --app-audio"),
            Item("Audio offset", "audio_offset", f"{values.get('audio_offset', 0.0)} s"),
        ]
        chosen = menu.choose("Audio", items, console=console)
        if chosen is CANCELLED:
            return

        if chosen in ("audio", "mic"):
            values[chosen] = not values.get(chosen, user_config.DEFAULTS[chosen])
        elif chosen in ("audio_device", "mic_device", "app_audio"):
            kind = {"audio_device": "audio", "mic_device": "mic", "app_audio": "app"}[chosen]
            title = {"audio_device": "System audio source", "mic_device": "Microphone",
                     "app_audio": "Application"}[chosen]
            picked = _pick_device(console, kind, title, values.get(chosen))
            if picked is not CANCELLED:
                values[chosen] = picked or None
        elif chosen in ("audio_gain", "mic_gain"):
            label = "System audio level" if chosen == "audio_gain" else "Microphone level"
            picked = _ask_number(console, f"{label} (1.0 = unchanged)",
                                 values.get(chosen, 1.0), whole=False)
            if picked is not CANCELLED:
                values[chosen] = picked
        elif chosen == "audio_offset":
            picked = _ask_number(console, "Audio offset in seconds",
                                 values.get("audio_offset", 0.0), whole=False)
            if picked is not CANCELLED:
                values[chosen] = picked


def _output_menu(console: Console, values: dict) -> None:
    while True:
        items = [
            Item("Save recordings in", "output_dir", _or_auto(values.get("output_dir"))),
            Item("Frame rate", "fps", str(values.get("fps", user_config.DEFAULTS["fps"]))),
            Item("Quality", "quality", str(values.get("quality", user_config.DEFAULTS["quality"]))),
        ]
        chosen = menu.choose("Output and quality", items, console=console)
        if chosen is CANCELLED:
            return

        if chosen == "output_dir":
            typed = menu.ask_text("Folder for recordings", console=console,
                                  current=values.get("output_dir") or "")
            if typed is not CANCELLED:
                values["output_dir"] = typed or None
        elif chosen == "fps":
            picked = _ask_number(console, "Frames per second",
                                 values.get("fps", user_config.DEFAULTS["fps"]), whole=True)
            if picked is not CANCELLED:
                values["fps"] = picked
        elif chosen == "quality":
            options = [Item(name, name, hint) for name, hint in (
                ("low", "smallest files"), ("balanced", "the default"), ("high", "best looking"))]
            current = values.get("quality", user_config.DEFAULTS["quality"])
            cursor = next((n for n, i in enumerate(options) if i.value == current), 1)
            picked = menu.choose("Quality", options, console=console, cursor=cursor)
            if picked is not CANCELLED:
                values["quality"] = picked


def run(console: Console) -> bool:
    """Edit the settings. Returns True if anything was written."""
    if not keys.interactive():
        console.print("[red]Editing settings needs an interactive terminal.[/]")
        return False

    path = user_config.config_path()
    original = dict(user_config.load(path).values)
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
        chosen = menu.choose(
            f"Settings  -  {path}", items, console=console,
            hint=menu.HINT_MENU + "   (nothing is saved until you choose Save)",
            on_left=False,
        )

        if chosen == "video":
            _video_menu(console, values)
        elif chosen == "audio":
            _audio_menu(console, values)
        elif chosen == "output":
            _output_menu(console, values)
        elif chosen in ("quit", CANCELLED):
            if values != original:
                confirm = menu.choose(
                    "Discard your changes?",
                    [Item("Keep editing", False), Item("Discard and quit", True)],
                    console=console,
                )
                if confirm is not True:
                    continue
            console.print("Nothing written.")
            return False
        elif chosen == "save":
            changes = {
                key: values.get(key)
                for key in user_config.DEFAULTS
                if values.get(key) != original.get(key)
            }
            if not changes:
                console.print("No changes to save.")
                return False
            user_config.set_values(changes, path)
            # Read it back so what is reported is what the file actually holds,
            # not what we believe we wrote.
            written = user_config.load(path)
            console.print(f"[green]Saved to[/] {path}")
            for key in sorted(changes):
                console.print(f"  {key} = {written.values.get(key, '[dim](default)[/]')}")
            for complaint in written.warnings:
                console.print(f"[yellow]{complaint}[/]")
            return True

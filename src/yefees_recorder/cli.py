"""yefees-recorder CLI entry point."""

from __future__ import annotations

import os
import platform
import shutil
import sys
import time
from importlib.metadata import version as _pkg_version
from datetime import datetime
from enum import Enum
from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table

from . import config as user_config
from . import editor, keys, menu, shutdown
from .capture import STARTUP_GRACE, get_backend, list_sources, parse_region

app = typer.Typer(help="Cross-platform screen recorder built on mpv.", no_args_is_help=True)
console = Console()

# Tools we shell out to, and how to get them per OS. (mpv was dropped as an
# engine - see CLAUDE.md; ffmpeg does both capture and encode.)
INSTALL_HINTS = {
    "Windows": {"ffmpeg": "winget install Gyan.FFmpeg"},
    "Darwin": {"ffmpeg": "brew install ffmpeg"},
    "Linux": {"ffmpeg": "sudo apt install ffmpeg", "wf-recorder": "sudo apt install wf-recorder"},
}


def required_tools() -> tuple[str, ...]:
    """ffmpeg everywhere, plus wf-recorder on Wayland where ffmpeg cannot capture."""
    on_wayland = (
        platform.system() == "Linux"
        and os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
    )
    return ("ffmpeg", "wf-recorder") if on_wayland else ("ffmpeg",)


def install_hint(tool: str, system: str | None = None) -> str:
    """Install command for `tool` on `system`, or a generic nudge on unknown platforms."""
    hints = INSTALL_HINTS.get(system or platform.system(), {})
    return hints.get(tool, f"install {tool} with your package manager")


class Quality(str, Enum):
    low = "low"
    balanced = "balanced"
    high = "high"


SELECT_WITH = {
    "display": "--display",
    "window": "--window",
    "audio": "--audio-device",
    "mic": "--mic-device",
    "app": "--app-audio",
}


def _version_callback(value: bool) -> None:
    if value:
        console.print(_pkg_version("yefees-recorder"))
        raise typer.Exit()


@app.callback()
def _main(
    _version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show version and exit."
    ),
) -> None:
    pass


@app.command()
def doctor() -> None:
    """Check that the tools needed to record on this system are installed."""
    table = Table(title="yefees-recorder doctor")
    table.add_column("tool")
    table.add_column("status")
    table.add_column("path / how to install")

    missing = []
    for tool in required_tools():
        path = shutil.which(tool)
        if path:
            table.add_row(tool, "[green]ok[/]", path)
        else:
            missing.append(tool)
            table.add_row(tool, "[red]missing[/]", install_hint(tool))

    if platform.system() == "Darwin":
        from .macos import screen_recording_permitted

        granted = screen_recording_permitted()
        if granted:
            table.add_row("screen recording", "[green]granted[/]", "")
        elif granted is None:
            table.add_row("screen recording", "[yellow]unknown[/]", "could not query CoreGraphics")
        else:
            missing.append("Screen Recording permission")
            table.add_row(
                "screen recording", "[red]denied[/]",
                "System Settings > Privacy & Security > Screen Recording"
                " (macOS captures nothing at all without it)",
            )

    console.print(table)
    if missing:
        console.print(f"[red]Missing: {', '.join(missing)}[/]")
        raise typer.Exit(1)
    console.print("[green]All good.[/]")


@app.command()
def record(
    output: Path = typer.Option(None, "-o", "--output", help="Output file (default: recording-<timestamp>.mp4)."),
    fps: int = typer.Option(None, "--fps", help="Capture framerate. [default: 30]"),
    audio: bool = typer.Option(None, "--audio/--no-audio", help="Capture system audio. [default: on]"),
    duration: float = typer.Option(0, "-d", "--duration", help="Stop after N seconds (0 = until you stop it)."),
    quality: Quality = typer.Option(None, "-q", "--quality", help="Encoding quality. [default: balanced]"),
    display: int = typer.Option(None, "--display", help="Record one monitor (see `sources`)."),
    window: str = typer.Option(None, "--window", help="Record one window by title (see `sources`)."),
    region: str = typer.Option(None, "--region", help="Record an area, as x,y,WIDTHxHEIGHT."),
    audio_device: str = typer.Option(None, "--audio-device", help="System audio source (see `sources`)."),
    mic: bool = typer.Option(None, "--mic/--no-mic", help="Also record the microphone. [default: off]"),
    mic_device: str = typer.Option(None, "--mic-device", help="Microphone to record (see `sources`)."),
    audio_gain: float = typer.Option(None, "--audio-gain", help="System audio level multiplier. [default: 1.0]"),
    mic_gain: float = typer.Option(None, "--mic-gain", help="Microphone level multiplier; mics are usually quiet. [default: 1.0]"),
    app_audio: str = typer.Option(None, "--app-audio", help="Record one application's audio (Linux only)."),
    audio_offset: float = typer.Option(None, "--audio-offset", help="Shift audio by N seconds if it drifts. [default: 0]"),
    pick: bool = typer.Option(False, "--pick", help="Choose what to record from a menu."),
    preset: str = typer.Option(None, "--preset", help="Use a saved preset (see `config`)."),
    save_preset: str = typer.Option(None, "--save-preset", help="Save these options under a name, then exit."),
) -> None:
    """Record the screen."""
    source_from_user = sum(x is not None for x in (display, window, region))
    if source_from_user > 1:
        console.print("[red]Pick only one of --display, --window and --region.[/]")
        raise typer.Exit(1)

    loaded = user_config.load()
    menu.apply_theme(loaded.values.get("accent"))
    for complaint in loaded.warnings:
        console.print(f"[yellow]{complaint}[/]")

    if save_preset:
        given = {
            key: value
            for key, value in (
                ("fps", fps), ("quality", quality.value if quality else None),
                ("audio", audio), ("audio_device", audio_device),
                ("mic", mic), ("mic_device", mic_device),
                ("audio_gain", audio_gain), ("mic_gain", mic_gain),
                ("app_audio", app_audio),
                ("audio_offset", audio_offset), ("display", display),
                ("window", window), ("region", region),
            )
            if value is not None
        }
        try:
            path = user_config.append_preset(save_preset, given)
        except (ValueError, OSError) as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1)
        console.print(f"[green]Saved preset[/] {save_preset!r} to {path}")
        return

    settings = dict(loaded.values)
    if preset:
        if preset not in loaded.presets:
            known = ", ".join(sorted(loaded.presets)) or "none saved yet"
            console.print(f"[red]No preset named {preset!r}. Available: {known}[/]")
            raise typer.Exit(1)
        settings.update(loaded.presets[preset])

    if pick:
        if source_from_user:
            console.print("[red]--pick chooses the source, so do not also pass one.[/]")
            raise typer.Exit(1)
        display, window = _pick_source()
        source_from_user = True

    # An explicit source (flag or menu) wins outright; otherwise take one from
    # the config or preset, where only one of the three may be set.
    if not source_from_user:
        display, window, region = (settings.get(k) for k in ("display", "window", "region"))
        if sum(x is not None for x in (display, window, region)) > 1:
            console.print("[red]Config sets more than one of display, window and region.[/]")
            raise typer.Exit(1)

    try:
        area = parse_region(region) if region else None
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)

    fps = user_config.resolve("fps", fps, settings)
    audio = user_config.resolve("audio", audio, settings)
    audio_offset = user_config.resolve("audio_offset", audio_offset, settings)
    audio_device = user_config.resolve("audio_device", audio_device, settings)
    mic = user_config.resolve("mic", mic, settings)
    mic_device = user_config.resolve("mic_device", mic_device, settings)
    audio_gain = user_config.resolve("audio_gain", audio_gain, settings)
    mic_gain = user_config.resolve("mic_gain", mic_gain, settings)
    app_audio = user_config.resolve("app_audio", app_audio, settings)
    quality_name = user_config.resolve("quality", quality.value if quality else None, settings)

    if output is None:
        directory = user_config.resolve("output_dir", None, settings)
        folder = Path(directory).expanduser() if directory else Path.cwd()
        output = folder / f"recording-{datetime.now():%Y%m%d-%H%M%S}.mp4"

    try:
        backend = get_backend(
            output, fps=fps, audio=audio, audio_offset=audio_offset,
            quality=quality_name, display=display, window=window,
            region=area, audio_device=audio_device, mic=mic, mic_device=mic_device,
            audio_gain=audio_gain, mic_gain=mic_gain, app_audio=app_audio,
            duration=duration,
        )
    except (NotImplementedError, ValueError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)

    try:
        backend.start()
    except RuntimeError as exc:  # missing wf-recorder, denied permission, ...
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)

    if audio and getattr(backend, "audio_error", None):
        console.print(f"[yellow]{backend.audio_error}[/]")
    if mic and getattr(backend, "mic_error", None):
        console.print(f"[yellow]{backend.mic_error}[/]")

    if keys.interactive():
        console.print("[green]Recording[/] - [bold]p[/] pause/resume, [bold]q[/] stop.")
    else:
        console.print("[green]Recording[/] - press Ctrl+C to stop.")

    # Closing the terminal must finish the file the same way Ctrl+C does, so
    # both paths go through one callback that can only run once.
    outcome: dict = {}
    finish = shutdown.install(lambda: _finish(backend, outcome))

    _run_until_stopped(backend, duration)
    console.print("Finishing up...")
    finish()

    if "error" in outcome:
        console.print(f"[red]{outcome['error']}[/]")
        raise typer.Exit(1)
    console.print(f"[green]Saved[/] {outcome['path']}")


PAUSE_KEYS = ("p", "P", " ")
STOP_KEYS = ("q", "Q", keys.ESC)


def _toggle_pause(backend, paused: bool) -> bool:
    """Pause or resume, and report it. Returns the state actually reached.

    A backend that refuses the change must not end the recording, so the old
    state is kept rather than raising.
    """
    try:
        backend.resume() if paused else backend.pause()
    except RuntimeError as exc:
        console.print(f"[yellow]{exc}[/]")
        return paused

    if paused:
        console.print("[green]Resumed.[/]")
    else:
        console.print("[yellow]Paused[/] - press p to resume.")
    return not paused


def _watch_for_keys(backend, deadline: float | None) -> None:
    """Handle pause and stop keys until one stops us, or the deadline passes."""
    paused = False
    while deadline is None or time.monotonic() < deadline:
        if backend.capture_ended:
            return  # it recorded its fill and stopped itself
        key = keys.read_key(0.2)
        if key in PAUSE_KEYS:
            paused = _toggle_pause(backend, paused)
        elif key in STOP_KEYS:
            return


def _finish(backend, outcome: dict) -> None:
    """Finalise the recording, recording the result for whoever asks after."""
    try:
        outcome["path"] = backend.stop()
    except (RuntimeError, OSError) as exc:
        outcome["error"] = exc


def _run_until_stopped(backend, duration: float) -> None:
    """Block until the user stops the recording, or `duration` runs out.

    Keys are read one at a time so pause and stop respond immediately without
    the user pressing Enter. Ctrl+C keeps working throughout, and is swallowed
    so the recording still gets finalised.
    """
    deadline = time.monotonic() + duration if duration > 0 else None
    if deadline is not None and backend.limits_duration:
        # It counts the duration from its first captured frame, so it finishes
        # later than this clock does; here the clock is only a backstop.
        deadline += STARTUP_GRACE
    try:
        with keys.raw_mode():
            _watch_for_keys(backend, deadline)
    except KeyboardInterrupt:
        pass


@app.command()
def sources() -> None:
    """List the displays, windows and audio devices available to record."""
    try:
        found = list_sources()
    except NotImplementedError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)

    if not found:
        console.print("[yellow]No sources found. Is ffmpeg installed? Try `doctor`.[/]")
        raise typer.Exit(1)

    table = Table(title="Recordable sources")
    table.add_column("kind")
    table.add_column("select with")
    table.add_column("details")
    for kind in ("display", "window", "audio", "mic", "app"):
        for source in (s for s in found if s.kind == kind):
            flag = (
                f"--display {source.id}" if kind == "display"
                else f'{SELECT_WITH[kind]} "{source.id}"'
            )
            # The label only earns a column when it says more than the id does.
            table.add_row(kind, flag, source.name if source.name != source.id else "")
    console.print(table)


def _pick_source() -> tuple[int | None, str | None]:
    """Show the source menu and return the chosen (display, window)."""
    if not sys.stdin.isatty():
        console.print("[red]--pick needs an interactive terminal.[/]")
        raise typer.Exit(1)
    try:
        found = [s for s in list_sources() if s.kind in ("display", "window")]
    except NotImplementedError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)

    items = [menu.Item("Whole desktop", None, "every monitor")]
    items += [
        menu.Item(source.id, source, source.name if source.name != source.id else source.kind)
        for source in found
    ]
    chosen = menu.choose("What should I record?", items, console=console,
                         hint=menu.HINT_MENU, on_left=False)
    if chosen is menu.CANCELLED:
        console.print("Cancelled.")
        raise typer.Exit(1)
    if chosen is None:
        return None, None
    return (int(chosen.id), None) if chosen.kind == "display" else (None, chosen.id)


@app.command()
def config(
    init: bool = typer.Option(False, "--init", help="Write a commented config file if none exists."),
    edit: bool = typer.Option(False, "-e", "--edit", help="Change settings interactively."),
) -> None:
    """Show, create or interactively edit the settings."""
    path = user_config.config_path()
    if init:
        if path.exists():
            console.print(f"[yellow]Already exists, leaving it alone:[/] {path}")
        else:
            console.print(f"[green]Created[/] {user_config.write_template()}")
        # Creating an empty file is rarely the point; offer to fill it in.
        if not edit and keys.interactive() and Confirm.ask("Edit the settings now?", default=True):
            edit = True

    if edit:
        editor.run(console)
        return

    loaded = user_config.load()
    settings = loaded.values
    for complaint in loaded.warnings:
        console.print(f"[yellow]{complaint}[/]")

    suffix = "" if path.exists() else "  (not created yet - run `config --init`)"
    table = Table(title=f"{path}{suffix}")
    table.add_column("setting")
    table.add_column("value")
    table.add_column("from")
    for key, default in user_config.DEFAULTS.items():
        configured = key in settings
        value = settings[key] if configured else default
        table.add_row(key, "-" if value is None else str(value), "config file" if configured else "default")
    console.print(table)

    if loaded.presets:
        saved = Table(title="Presets  (use with: record --preset NAME)")
        saved.add_column("name")
        saved.add_column("settings")
        for name, body in sorted(loaded.presets.items()):
            saved.add_row(name, ", ".join(f"{k}={v}" for k, v in sorted(body.items())))
        console.print(saved)

    console.print("[dim]Change these with: yefees-recorder config --edit[/]")


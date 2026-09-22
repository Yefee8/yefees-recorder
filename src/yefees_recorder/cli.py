"""yefees-recorder CLI entry point."""

from __future__ import annotations

import os
import platform
import shutil
import time
from importlib.metadata import version as _pkg_version
from datetime import datetime
from enum import Enum
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .capture import get_backend, list_sources, parse_region

app = typer.Typer(help="Cross-platform screen recorder built on mpv.", no_args_is_help=True)
console = Console()

# Tools we shell out to, and how to get them per OS. (mpv was dropped as an
# engine — see CLAUDE.md; ffmpeg does both capture and encode.)
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


SELECT_WITH = {"display": "--display", "window": "--window", "audio": "--audio-device"}


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
                " (macOS records black frames without it)",
            )

    console.print(table)
    if missing:
        console.print(f"[red]Missing: {', '.join(missing)}[/]")
        raise typer.Exit(1)
    console.print("[green]All good.[/]")


@app.command()
def record(
    output: Path = typer.Option(None, "-o", "--output", help="Output file (default: recording-<timestamp>.mp4)."),
    fps: int = typer.Option(30, "--fps", help="Capture framerate."),
    audio: bool = typer.Option(True, "--audio/--no-audio", help="Capture system audio."),
    duration: float = typer.Option(0, "-d", "--duration", help="Stop after N seconds (0 = until Ctrl+C)."),
    quality: Quality = typer.Option(Quality.balanced, "-q", "--quality", help="Encoding quality."),
    display: int = typer.Option(None, "--display", help="Record one monitor (see `sources`)."),
    window: str = typer.Option(None, "--window", help="Record one window by title (see `sources`)."),
    region: str = typer.Option(None, "--region", help="Record an area, as x,y,WIDTHxHEIGHT."),
    audio_device: str = typer.Option(None, "--audio-device", help="Audio source to record (see `sources`)."),
    audio_offset: float = typer.Option(0.0, "--audio-offset", help="Shift audio by N seconds if it drifts on your machine."),
) -> None:
    """Record the screen."""
    if output is None:
        output = Path(f"recording-{datetime.now():%Y%m%d-%H%M%S}.mp4")
    if sum(x is not None for x in (display, window, region)) > 1:
        console.print("[red]Pick only one of --display, --window and --region.[/]")
        raise typer.Exit(1)
    try:
        area = parse_region(region) if region else None
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)

    try:
        backend = get_backend(
            output, fps=fps, audio=audio, audio_offset=audio_offset,
            quality=quality.value, display=display, window=window,
            region=area, audio_device=audio_device,
        )
    except NotImplementedError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)

    try:
        backend.start()
    except RuntimeError as exc:  # missing wf-recorder, denied permission, ...
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)

    if audio and getattr(backend, "audio_error", None):
        console.print(f"[yellow]{backend.audio_error}[/]")
        console.print("[yellow]Recording video only.[/]")
    console.print("[green]Recording[/] - press Ctrl+C to stop.")
    try:
        if duration > 0:
            time.sleep(duration)
        else:
            while True:
                time.sleep(0.2)
    except KeyboardInterrupt:
        pass

    console.print("Finishing up...")
    try:
        console.print(f"[green]Saved[/] {backend.stop()}")
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)


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
    for kind in ("display", "window", "audio"):
        for source in (s for s in found if s.kind == kind):
            flag = (
                f"--display {source.id}" if kind == "display"
                else f'{SELECT_WITH[kind]} "{source.id}"'
            )
            # The label only earns a column when it says more than the id does.
            table.add_row(kind, flag, source.name if source.name != source.id else "")
    console.print(table)

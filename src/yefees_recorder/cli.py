"""yefees-recorder CLI entry point."""

from __future__ import annotations

import platform
import shutil
import time
from importlib.metadata import version as _pkg_version
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .capture import get_backend

app = typer.Typer(help="Cross-platform screen recorder built on mpv.", no_args_is_help=True)
console = Console()

# Tools we shell out to, and how to get them per OS. (mpv was dropped as an
# engine — see CLAUDE.md; ffmpeg does both capture and encode.)
INSTALL_HINTS = {
    "Windows": {"ffmpeg": "winget install Gyan.FFmpeg"},
    "Darwin": {"ffmpeg": "brew install ffmpeg"},
    "Linux": {"ffmpeg": "sudo apt install ffmpeg"},
}


def install_hint(tool: str, system: str | None = None) -> str:
    """Install command for `tool` on `system`, or a generic nudge on unknown platforms."""
    hints = INSTALL_HINTS.get(system or platform.system(), {})
    return hints.get(tool, f"install {tool} with your package manager")


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
    """Check that ffmpeg is installed."""
    table = Table(title="yefees-recorder doctor")
    table.add_column("tool")
    table.add_column("status")
    table.add_column("path / how to install")

    missing = []
    for tool in ("ffmpeg",):
        path = shutil.which(tool)
        if path:
            table.add_row(tool, "[green]ok[/]", path)
        else:
            missing.append(tool)
            table.add_row(tool, "[red]missing[/]", install_hint(tool))

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
    audio_offset: float = typer.Option(0.0, "--audio-offset", help="Shift audio by N seconds if it drifts on your machine."),
) -> None:
    """Record the screen."""
    if output is None:
        output = Path(f"recording-{datetime.now():%Y%m%d-%H%M%S}.mp4")
    try:
        backend = get_backend(output, fps=fps, audio=audio, audio_offset=audio_offset)
    except NotImplementedError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)

    backend.start()
    if audio and getattr(backend, "audio_error", None):
        console.print("[yellow]No system audio device; recording video only.[/]")
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

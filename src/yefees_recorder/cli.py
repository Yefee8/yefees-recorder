"""yefees-recorder CLI entry point."""

from __future__ import annotations

import platform
import shutil
from importlib.metadata import version as _pkg_version

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Cross-platform screen recorder built on mpv.", no_args_is_help=True)
console = Console()

# Tools we shell out to, and how to get them per OS.
INSTALL_HINTS = {
    "Windows": {"mpv": "winget install mpv", "ffmpeg": "winget install Gyan.FFmpeg"},
    "Darwin": {"mpv": "brew install mpv", "ffmpeg": "brew install ffmpeg"},
    "Linux": {"mpv": "sudo apt install mpv", "ffmpeg": "sudo apt install ffmpeg"},
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
    """Check that mpv and ffmpeg are installed."""
    table = Table(title="yefees-recorder doctor")
    table.add_column("tool")
    table.add_column("status")
    table.add_column("path / how to install")

    missing = []
    for tool in ("mpv", "ffmpeg"):
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

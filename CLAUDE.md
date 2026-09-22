# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Pre-code. The repo has no commits and no source yet — only `plan.md` (gitignored, Turkish), which is the authoritative spec. **Read `plan.md` before starting any work here**; it holds the library choices, the platform matrix, and the phase order. Update this file as real commands and modules land.

## What this is

`yefees-recorder` — a cross-platform (Linux/macOS/Windows) screen-recording CLI in Python, driving **mpv** as the capture engine rather than shelling out to ffmpeg directly.

## Toolchain (planned, per plan.md)

`uv` for packaging (`uv init --package`, `uv build`, `uv publish` via PyPI Trusted Publishing from GitHub Actions). Runtime deps: `typer` (CLI), `rich` (output), `blessed` (live pause/stop hotkeys during recording), `python-mpv` or raw mpv IPC, `platformdirs` + config lib, `PyAudioWPatch` (Windows only).

## Architecture constraints worth knowing up front

- **mpv is the engine, not a library dependency.** mpv opens capture devices via `av://` (libavdevice: x11grab, PipeWire, gdigrab, avfoundation) and writes with `--stream-record`. It is controlled by launching it with `--input-ipc-server=<socket>` and sending JSON IPC commands for start/pause/stop.
- **mpv and ffmpeg are NOT bundled** into the pip package (size + GPL licensing). A `doctor` command checks for them and prints the OS-appropriate install command instead.
- **Backends are per-OS, selected at runtime.** A `CaptureBackend` interface (`video_source`, `audio_source`, `start`, `pause`, `stop`) with `LinuxX11Backend` / `LinuxWaylandBackend` / `MacBackend` / `WindowsBackend`. Dispatch on `platform.system()`, plus `XDG_SESSION_TYPE` to split X11 from Wayland.
- **Windows audio bypasses mpv entirely.** dshow has no native loopback, so system audio is captured separately via `PyAudioWPatch` (WASAPI loopback) to its own file and muxed with the video afterwards. Don't try to route it through mpv.
- **macOS has no native system-audio loopback** — it needs a virtual device like BlackHole. First run also needs Screen Recording (TCC) permission; without it capture silently records black frames, so detect and explain rather than failing mutely.
- **Wayland source selection cannot be automated** — the `xdg-desktop-portal` dialog is an OS security boundary and the user must pick the screen/window there.

## Working style for this repo

Phases in `plan.md` §5 are meant to be separate commits, and the three backend phases (1a Linux, 1b macOS, 1c Windows) are explicitly meant to be built and tested one at a time — do not debug all three platforms in one pass.

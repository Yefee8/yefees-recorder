# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Phases 0, 1c (Windows) and 1a (Linux) are done. macOS (1b) is not implemented — `get_backend()` raises `NotImplementedError` for it.

**Windows is the only backend verified against real hardware.** The Linux backends are unit-tested at the command-construction level only; nobody has yet run an actual X11 or Wayland capture. Treat the first real Linux run as a debugging session, not a smoke test.

`plan.md` (gitignored, Turkish) holds the phase order and remains the roadmap, **but its central technical premise turned out to be wrong — see "Why not mpv" below.** Trust this file over `plan.md` on engine choice.

## What this is

`yefees-recorder` — a cross-platform (Linux/macOS/Windows) screen-recording CLI in Python, driving **ffmpeg** as the capture and encode engine, except on Wayland where ffmpeg cannot capture at all.

## Commands

```
uv sync
uv run yefees-recorder doctor
uv run yefees-recorder record -d 5 -o out.mp4
uv run pytest
uv run pytest tests/test_capture.py::test_pause_skips_the_gap
```

`tests/test_capture.py` drives the real engine against ffmpeg's `lavfi testsrc` instead of a real screen, so the lifecycle is testable headlessly. **That fake source needs `-re`** — without it lavfi generates frames as fast as it can and a 1.5 s test yields ~700 s of video.

## Why not mpv (measured, do not retry)

`plan.md` specifies mpv + `--stream-record` + `av://`. That does not work:

- The protocol is `avdevice://`, not `av://`.
- `mpv avdevice://gdigrab:desktop` opens the device, then every frame fails with `gdigrab: Failed to capture image (error 6)` (invalid handle — GDI handles are thread-affine and mpv's demuxer is not). `--demuxer-thread=no` does not help. The same command under plain ffmpeg works fine.
- `--stream-record` remuxes, it does not encode. Against a capture device the stream is raw BMP/rawvideo, and mpv logs `Writing header failed` and emits a 393-byte file. Even if it worked it would write ~250 MB/s uncompressed. **This part is platform-independent**, so x11grab and avfoundation will hit it too — assume 1a and 1b also use ffmpeg.

ffmpeg is stopped gracefully by writing `q` to its stdin (returns 0, finalizes the file); it is spawned with `CREATE_NEW_PROCESS_GROUP` on Windows so a Ctrl+C in our terminal doesn't reach it. A backend whose recorder isn't ffmpeg sets `stop_signal` instead, and the lifecycle sends that signal rather than writing `q`.

## Why Wayland can't use ffmpeg (researched, do not retry)

Wayland gives no direct screen access: it goes through `xdg-desktop-portal` over D-Bus and comes back as a PipeWire stream. The `pipewiregrab` patches that would have let ffmpeg read that stream were posted in 2023 and 2024 and **never merged — they are absent even from ffmpeg 8.1**. There is no shell-only portal handshake either, because `OpenPipeWireRemote` returns a file descriptor.

So `LinuxWaylandBackend` shells out to `wf-recorder` and stops it with SIGINT. That covers wlroots compositors (Sway, Hyprland, river) and **not GNOME or KDE** — supporting those properly means writing a real D-Bus portal client plus a PipeWire consumer, which is a project of its own, not a patch.

## Architecture

`capture.py` owns the whole recording lifecycle; a backend subclass only answers *what* to capture. To add Linux or macOS, implement `video_input_args()` (and `make_audio_recorder()` if the platform has system audio) and register it in `get_backend()` — nothing else should need touching.

- **ffmpeg is not bundled** into the pip package (size + GPL licensing). `doctor` checks for it and prints the OS-appropriate install command.
- **Pause is implemented as segmentation.** ffmpeg has no pause. `pause()` ends the current ffmpeg process, `resume()` starts a new one, and `stop()` muxes each segment with its audio and concatenates them with `-c copy`. Wall-clock time spent paused therefore never reaches the output. Nothing calls `pause()` yet — hotkeys are phase 4; it is exercised by tests.
- **Segments are `.mkv` internally** (survives an abrupt kill) and only the final concat writes the user's chosen extension.
- **Audio arrives one of two ways.** If `audio_input_args()` is non-empty the platform can capture system audio through ffmpeg itself and it rides along as a second input (Linux: `-f pulse -i <sink>.monitor`). If it's empty, `make_audio_recorder()` runs a side recorder whose file is muxed in on stop (Windows). Don't mix the two on one backend.
- **Windows audio does not come from ffmpeg.** dshow exposes no loopback device (verified: `-list_devices` shows only a microphone), so `PyAudioWPatch` captures WASAPI loopback to a wav that is muxed in afterwards.
- **Record the sink's `.monitor`, not the default source** — the plain default PulseAudio source is the microphone, not system audio.
- **The loopback wav is built against wall clock, not against the device.** Windows only feeds a loopback stream while something is actually playing — on a fully silent machine the callback never fires at all, and mid-recording silence produces gaps. `WasapiLoopbackRecorder` therefore pads with real silence up to the elapsed-time position on every callback and again at stop. Removing that padding silently desyncs audio from video. Measured after the fix: beeps 4.000 s apart in the source land 3.998 / 4.001 / 3.998 s apart in the output.
- **`--audio-offset` is a deliberate calibration knob**, not dead config: residual constant A/V offset depends on how fast a given machine's audio endpoint spins up.
- **macOS has no native system-audio loopback** — it needs a virtual device like BlackHole. First run also needs Screen Recording (TCC) permission; without it capture silently records black frames, so detect and explain rather than failing mutely.
- **Wayland source selection cannot be automated** — the `xdg-desktop-portal` dialog is an OS security boundary and the user must pick the screen/window there.
- Backend selection is `platform.system()` plus `XDG_SESSION_TYPE` on Linux, in `get_backend()`.

## Working style for this repo

Phases in `plan.md` §5 are separate commits, and the backend phases are built and tested one at a time — do not debug all three platforms in one pass. 1c was done before 1a/1b because the only machine available is Windows.

Verify recorder changes by measurement, not by "it produced a file": check both stream durations with `ffprobe` and check A/V alignment with a known-spacing tone (`aevalsrc` + `silencedetect`). Several bugs here produced perfectly valid files with wrong or missing audio.

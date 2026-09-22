# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Phases 1 (backends), 2 (source and quality selection), 5 (CI matrix) and 6 (release plumbing) are done. Phases 3 and 4 (config file, hotkeys) are not started. The remote is `github.com/Yefee8/yefees-recorder`, but nothing has been pushed to it and nothing has been published — neither workflow has ever run.

Verification status per platform:

| | Screen capture | Where it is proven |
|---|---|---|
| Windows | verified on real hardware | local runs; `tests/test_windows.py` |
| Linux X11 | verified in CI | `tests/test_linux_capture.py` under Xvfb |
| Linux Wayland | **never run** | command construction only |
| macOS | **never run** — a hosted runner cannot hold Screen Recording permission | parser smoke test only |

Wayland and macOS capture remain unproven; treat a first real run on either as a debugging session.

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
- **macOS records black frames rather than erroring when Screen Recording permission is missing.** `screen_recording_permitted()` checks `CGPreflightScreenCaptureAccess` through ctypes (no dependency) before capture starts. It returns `None` when it cannot tell — treat only an explicit `False` as denied, or non-macOS machines would refuse to record. Granting permission only affects newly launched processes, so the terminal has to be restarted.
- **macOS has no native system-audio loopback** — it needs a virtual device like BlackHole, which appears as an avfoundation *input*. Missing one downgrades to video-only rather than failing.
- **avfoundation uses two separate inputs**, `screen:none` and `none:audio`, not the combined `screen:audio` form, which drifts between the streams.
- **Device indices are discovered, never hardcoded** — `-list_devices` writes to stderr and exits non-zero by design, so the exit code is ignored and stderr is parsed. The camera is usually index 0 and the screen 1, and the microphone sits at audio 0 ahead of the loopback device, so picking index 0 gets you a webcam and a mic.
- **Wayland source selection cannot be automated** — the `xdg-desktop-portal` dialog is an OS security boundary and the user must pick the screen/window there.
- Backend selection is `platform.system()` plus `XDG_SESSION_TYPE` on Linux, in `get_backend()`.

## Source selection

`--display`, `--window` and `--region` are mutually exclusive and resolve very differently per platform, which is why each backend does its own thing rather than sharing a region helper:

| | display | window | region |
|---|---|---|---|
| Windows | `-offset_x/-offset_y/-video_size` from `EnumDisplayMonitors` | native `-i title=...` | same offset args |
| Linux X11 | offset appended to `DISPLAY` as `:0+x,y` | `-window_id` from `wmctrl` | same |
| Linux Wayland | unsupported | unsupported | `wf-recorder -g` |
| macOS | a different avfoundation device | **impossible** | `crop` filter after capture |

Things that bite here:

- **Windows window enumeration must skip DWM-cloaked windows.** Suspended UWP apps (Settings, Movies & TV) stay `IsWindowVisible`, so without the `DWMWA_CLOAKED` check the picker fills with duplicate entries for apps that aren't on screen — measured: 18 raw windows down to 11 real ones.
- **avfoundation video index 0 is usually a webcam and audio index 0 a microphone.** `--display N` counts screens, not devices, and is mapped through `screen_device_indices()`.
- **Monitor offsets can be negative** on both Windows and X11 when a monitor sits left of or above the primary, so `parse_region` and the xrandr regex both accept a leading `-`.
- **`--window` with x11grab must not also pass `-video_size`** — the window's own size wins.

Quality is `low`/`balanced`/`high` mapping to an x264 preset plus CRF. Even `high` stays at `medium` rather than a slow preset: capture is realtime, and dropping frames costs more than bitrate does. Measured on 4s of 1920x1080: 127 / 278 / 323 KiB.

## CI

`.github/workflows/ci.yml` runs Linux (3.10 floor and 3.13), macOS and Windows. Things worth knowing before editing it:

- **`pyaudiowpatch` is marked `sys_platform == 'win32'`.** It publishes Windows-only wheels, so without the marker Linux and macOS try to build PyAudio from source and the install fails.
- **The Linux job runs pytest under `xvfb-run`.** That `DISPLAY` is the entire reason `tests/test_linux_capture.py` executes rather than skipping — it paints the root window red with `xsetroot` and asserts the captured frames are actually red, because a recorder failure here yields a valid file full of black frames, not an error.
- **The macOS `doctor` step is allowed to fail.** A hosted runner cannot be granted Screen Recording permission, so `doctor` correctly exits non-zero there. Don't "fix" that by weakening `doctor`.
- `uv sync --locked` fails the build if `pyproject.toml` changed without relocking.

## Releasing

`.github/workflows/release.yml` fires on a `v*` tag: it checks the tag against `uv version`, builds, publishes to PyPI over OIDC (no API token anywhere), then opens a GitHub Release.

**Three things must be set up before the first tag, or it will fail:**

1. A **pending** Trusted Publisher on PyPI (pypi.org > Your projects > Publishing). It must be "pending" because the project does not exist on PyPI yet — a normal trusted publisher can only be added to a project that already has a release. Fill in repo owner, repo name, workflow `release.yml`, environment `pypi`.
2. A GitHub environment named **`pypi`**, matching the `environment:` in the workflow.
3. Nothing else — `project.urls` points at `github.com/Yefee8/yefees-recorder`, taken from the configured remote.

The name `yefees-recorder` was free on PyPI as of the phase 6 commit.

The version lives only in `pyproject.toml`; `--version` reads it back through `importlib.metadata`, so there is no second copy to keep in sync. The license is declared as a PEP 639 expression, which means **adding a `License ::` classifier would be an error** — PyPI rejects a classifier paired with a license expression.

## Working style for this repo

Phases in `plan.md` §5 are separate commits, and the backend phases are built and tested one at a time — do not debug all three platforms in one pass. 1c was done before 1a/1b because the only machine available is Windows.

Errors meant for the user (missing wf-recorder, denied permission) are raised as `RuntimeError` from `capture_command`, which surfaces at `backend.start()`. `cli.record` catches `RuntimeError` there — without that catch the carefully written help text prints as a traceback.

Verify recorder changes by measurement, not by "it produced a file": check both stream durations with `ffprobe` and check A/V alignment with a known-spacing tone (`aevalsrc` + `silencedetect`). Several bugs here produced perfectly valid files with wrong or missing audio.

# macOS session: what happened

This was a handoff plan for bringing up the macOS backend, which had never been
run. It has been run now. The plan is kept below for the record; everything
durable from it has moved into `CLAUDE.md`, which is where to look first.

Run on macOS 14.5 (Apple silicon, one Retina display at 1710x1112 points /
3420x2224 pixels) with ffmpeg 8.1.2 from Homebrew and BlackHole 2ch.

## The test order, and how it went

| # | Step | Result |
|---|---|---|
| 1 | `doctor` | passed once permission was granted |
| 2 | `sources` | passed unchanged — the parser handled ffmpeg 8.1.2's extra noise |
| 3 | `record --no-audio` | passed after two fixes (pixel format, duration) |
| 4 | denied permission | passed — and the assumption behind it was wrong |
| 5 | system audio | **failed badly**, three separate faults |
| 6 | microphone | passed on device selection; "audible" not verified |
| 7 | both mixed | failed with 5, passed after |
| 8 | `--display 1` | passed — single-monitor Mac, out-of-range error |
| 9 | `--region` | passed; Retina caveat confirmed and documented |
| 10 | `--window` | passed — refuses, points at `--region` |
| 11 | pause / resume | passed, and exposed an audio alignment bug |
| 12 | terminal closed | passed (SIGHUP and SIGTERM) |
| 13 | `config --edit` | **failed** — arrow keys had never worked on POSIX |

## What it cost

Eight bugs, four of them in code shared with Linux:

- `keys.raw_mode()` raised on a captured stdin, failing 27 tests on any POSIX machine.
- `doctor`'s "all tools present" test depended on the machine having Screen Recording.
- **Arrow keys never worked in any menu on macOS or Linux** — a buffered read left the escape sequence where `select` could not see it, so every arrow read as Esc and desynchronised the stream for good.
- `read_key` spun at 382,637 calls a second once its input ended.
- macOS asked the capture device for a pixel format no screen offers, printing an error block on every recording.
- `--duration` came out a second short, because avfoundation needs 1.3s to produce its first frame.
- Audio lost a quarter of its samples, could not share a process with the screen, and could not be mixed live.
- The audio and the video did not start together, and which one was first varied by segment.

## Still not verified

- **"Audible" in steps 6 and 7.** The device selection, levels and timing are all measured, but the machine's output was muted, so nothing was played through the speakers and heard back. The absolute A/V offset is only bounded indirectly, by the two streams agreeing to 0.07s.
- **`--display 1` on a real second monitor.** Only the out-of-range error was exercised.
- **`--app-audio`**, which macOS refuses by design.
- **Wayland**, which remains the one backend nobody has ever run.

## The original plan

Read `CLAUDE.md` first — this only covers what is specific to finishing macOS.

### Setup

```bash
brew install ffmpeg
uv sync
uv run yefees-recorder doctor
```

Then grant **Screen Recording** to the terminal app: System Settings → Privacy
& Security → Screen Recording, tick Terminal/iTerm, and **restart the terminal**
— macOS only applies the grant to newly launched processes.

System audio additionally needs a loopback device:

```bash
brew install blackhole-2ch
```

then in Audio MIDI Setup create a Multi-Output Device containing BlackHole *and*
the speakers, and select it as the system output. Without the multi-output you
record the audio but stop hearing it.

### The parser was expected to be the most fragile part

It was not. `list_avfoundation_devices()` scrapes
`ffmpeg -f avfoundation -list_devices` from stderr, and ffmpeg 8.1.2 wraps it in
more noise than the old sample had — an ObjC camera warning before the sections
and an `[in#0 @ 0x...] Error opening input` line *inside* one. Neither confused
`DEVICE_LINE`. The sample in `tests/test_macos.py` carries that noise now.

### Things the plan flagged, and what they turned out to be

- **Retina scaling** — real. `--region` crops in backing pixels, so a region
  looks half the intended size. Documented in the README rather than doubled.
- **`-capture_cursor 1`** — accepted, no change needed.
- **Framerate** — the plan expected `-framerate` to be refused. avfoundation
  logs `Configuration of video device failed` no matter what it is given, and
  applies the framerate anyway: 5 / 15 / 30 / 60 all landed within 0.3 fps.
- **Pixel format** — the real problem, though not the one expected. See
  CLAUDE.md.
- **Permission returning `None`** — untouched, still only `False` counts.
- **Per-application audio** — still impossible, still says so.
- **Terminal colours** — the selection renders as bold white on the indigo, one
  block across the whole row, and `readable_on()` picks the foreground.

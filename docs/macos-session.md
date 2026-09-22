# macOS session plan

Hand this file to a session running on the Mac. Read `CLAUDE.md` first — this
only covers what is specific to finishing macOS.

## Where things stand

`src/yefees_recorder/macos.py` is fully written and **has never been run**.
Every other platform note in `CLAUDE.md` applies; the macOS-specific claims in
it are reasoning, not measurements. Treat this as a debugging session, not a
smoke test.

Windows is verified on real hardware. Linux X11 is verified in CI under Xvfb.
Linux Wayland and macOS are the two unverified backends.

## Setup

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

## Test order

Work down this list. Each step is written so a failure tells you which layer
broke. Stop and fix before moving on — later steps assume earlier ones work.

| # | Command | Expected | If it fails |
|---|---------|----------|-------------|
| 1 | `doctor` | ffmpeg ok, screen recording granted | The permission row is the thing to trust; `granted` comes from `CGPreflightScreenCaptureAccess` |
| 2 | `sources` | displays, audio, mic listed | **Highest-risk step** — see "The parser" below |
| 3 | `record -d 5 --no-audio -o v.mp4` | ~5s, screen resolution | If black frames: permission, not code |
| 4 | Revoke permission, `record` | Clear error, system prompt appears | Should never silently record black |
| 5 | `record -d 5 -o a.mp4` | video + aac, audible system audio | Needs BlackHole selected as output |
| 6 | `record -d 5 --mic --no-audio` | mic track, audible | Check it picked the mic, not BlackHole |
| 7 | `record -d 5 --mic` | one mixed track, both audible | Verify system audio is not quieter than in step 5 |
| 8 | `record -d 5 --display 1` | the second monitor | Single-monitor Macs: expect the out-of-range error |
| 9 | `record -d 5 --region 0,0,640x480` | 640x480 | Retina — see below |
| 10 | `record --window Safari` | Refuses, points at `--region` | This is intended, not a bug |
| 11 | `record` then `p`, `p`, `q` | paused time cut out | Exercises segment + concat |
| 12 | `record`, then close the terminal window | file still playable | SIGHUP path |
| 13 | `config --edit` | menus readable, arrows work | Colours: see below |

## The parser is the most fragile part

`list_avfoundation_devices()` scrapes `ffmpeg -f avfoundation -list_devices`
from **stderr**, and that command exits non-zero by design. Run it by hand
first and compare with what `sources` prints:

```bash
ffmpeg -hide_banner -f avfoundation -list_devices true -i ""
```

If the format has drifted, fix `DEVICE_LINE` in `macos.py` and update the
verbatim sample in `tests/test_macos.py` — that fixture is the regression test.

Remember the indices: **video 0 is usually the FaceTime camera and audio 0 the
built-in mic**, which is why `--display N` counts screens through
`screen_device_indices()` rather than using the device index directly.

## macOS-specific things to check

- **Retina scaling.** avfoundation captures at the backing resolution, so a
  "1440x900" display may record as 2880x1800. `--region` crops in *pixels*, not
  points, so a region that looks right in Screenshot will be half the intended
  size. Find out which it is in step 9 and, if it bites, document it in the
  README rather than silently doubling numbers.
- **`-capture_cursor 1`** is passed unconditionally and has never been run. If
  ffmpeg rejects it, drop it or make it a flag.
- **Framerate.** avfoundation may refuse arbitrary `-framerate` values on some
  displays. If step 3 errors, try without `-framerate` before blaming anything
  else.
- **Pixel format.** avfoundation hands over `uyvy422`; the pipeline converts to
  `yuv420p`. If ffmpeg complains about the conversion, that is where to look.
- **Permission returning `None`.** `screen_recording_permitted()` returns
  `None` when it cannot tell. Only an explicit `False` counts as denied — do
  not "fix" it to treat `None` as denied, or non-macOS machines stop recording.
- **Per-application audio is not possible** and should stay that way: it needs
  Core Audio process taps, which ffmpeg cannot reach. The error already names
  the virtual-device workaround.
- **Terminal colours.** The menu accent is indigo `#5A4FCF` with the text
  colour derived from its brightness. Terminal.app's default profile is dark;
  if a light profile makes the selection unreadable, set a lighter accent
  (`config --edit` → Menu colour) rather than changing the default for
  everyone.

## What to do with findings

- Fix in `macos.py`, keep `tests/test_macos.py` passing, add a test for
  anything the real output contradicted.
- Update the verification table in `CLAUDE.md` — macOS should stop saying
  "never run" once it has been.
- Run the whole suite before committing: `uv run pytest`.
- CI has a macOS job, but a hosted runner can never hold Screen Recording
  permission, so its `doctor` step is deliberately allowed to fail. Do not
  weaken `doctor` to make CI green.

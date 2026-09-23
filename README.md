# yefees-recorder

Cross-platform screen recorder CLI for Windows, Linux and macOS. It drives
**ffmpeg** and adds the parts ffmpeg does not have: picking a source, pausing,
mixing system audio with a microphone at sane levels, finishing the file when
the terminal goes away, and a settings menu so none of it needs flags.

| Platform | Screen | System audio | Microphone |
|---|---|---|---|
| Windows | gdigrab | WASAPI loopback, no virtual cable needed | dshow |
| Linux / X11 | x11grab | PulseAudio / PipeWire sink monitor | PulseAudio source |
| Linux / Wayland | wf-recorder (wlroots only — not GNOME/KDE) | sink monitor | one source only |
| macOS | avfoundation | BlackHole or another virtual device | avfoundation |

## What has actually been tested

Worth knowing before you rely on it:

| | Screen | Audio |
|---|---|---|
| **Windows** | recorded on real hardware | recorded on real hardware |
| **macOS** | recorded on real hardware (14.5) | recorded on real hardware |
| **Linux / X11** | **not tested** | **not tested** |
| **Linux / Wayland** | **never run at all** | **never run at all** |

**Nobody working on this has a Linux machine.** The Linux backends are written
and covered by unit tests, and CI runs the X11 capture test against a virtual
display — where it currently **fails**: it paints the screen red, records it,
and gets black frames back. That failure is honest rather than hidden, because a
broken recorder here produces a perfectly valid file full of black frames and no
error at all. Until someone runs it on a real Linux desktop, treat Linux as
unproven.

Bug reports from a real Linux session are very welcome, and the most useful ones
say what `yefees-recorder doctor` printed and whether the recorded file is black.

## Installing

### 1. ffmpeg

ffmpeg is **not** bundled — it is large and its licensing is its own — so
install it first:

```bash
winget install Gyan.FFmpeg     # Windows
brew install ffmpeg            # macOS
sudo apt install ffmpeg        # Debian / Ubuntu
sudo pacman -S ffmpeg          # Arch
```

On Wayland you also need `wf-recorder`, because ffmpeg cannot capture a Wayland
screen at all:

```bash
sudo apt install wf-recorder
```

### 2. The recorder

```bash
pip install yefees-recorder
```

or, if you use [uv](https://docs.astral.sh/uv/), which puts it on your PATH in
its own isolated environment:

```bash
uv tool install yefees-recorder
```

There is one package for every platform — it is pure Python, so there is no
Windows build or Linux build to choose between. The one platform-specific
dependency (`pyaudiowpatch`, for Windows loopback audio) is marked as such and
is only downloaded on Windows.

### 3. Check it

```bash
yefees-recorder doctor
```

It reports whether ffmpeg is on your PATH, and on macOS whether Screen Recording
permission has been granted. Fix anything it complains about before recording.

## Running from a clone

If you cloned the repository instead of installing the package:

```bash
git clone https://github.com/Yefee8/yefees-recorder.git
cd yefees-recorder
uv sync
uv run yefees-recorder doctor
```

`uv sync` creates `.venv` and installs the dependencies pinned in `uv.lock`;
`uv run` then executes inside it, so nothing is installed system-wide. Every
command below works the same way — put `uv run` in front:

```bash
uv run yefees-recorder record -d 10 -o clip.mp4
uv run pytest
```

Don't have uv? [Install it](https://docs.astral.sh/uv/getting-started/installation/),
or use the standard library:

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
yefees-recorder doctor
```

Python 3.10 or newer is required.

## Usage

```bash
yefees-recorder doctor                  # check ffmpeg is installed
yefees-recorder sources                 # list displays, windows and audio devices
yefees-recorder record                  # record until you press q
yefees-recorder record -d 30 -o clip.mp4
yefees-recorder record --no-audio --fps 60
```

### Choosing what to record

`sources` prints the exact flag for each thing it finds:

```bash
yefees-recorder record --display 1                   # one monitor
yefees-recorder record --window "Firefox"            # one window
yefees-recorder record --region 0,0,1280x720         # an area, as x,y,WIDTHxHEIGHT
yefees-recorder record --audio-device "Speakers"     # a specific audio source
yefees-recorder record --mic                         # add the microphone
yefees-recorder record --mic --no-audio              # microphone only
yefees-recorder record --mic-device "Headset"        # a specific microphone
yefees-recorder record --mic-gain 4                  # mics are quiet; turn it up
yefees-recorder record -q high                       # low | balanced | high
yefees-recorder record --pick                        # choose from a menu instead
```

`--display`, `--window` and `--region` are mutually exclusive — only one thing
can be recorded at a time. Recording the whole desktop is the default, which on
a multi-monitor machine means every monitor side by side; use `--display` for
one of them.

Window capture is not available everywhere: Windows does it natively, X11 needs
`wmctrl` installed, and macOS cannot do it at all.

### While recording

Press **p** (or space) to pause and resume, **q** to stop. Ctrl+C also stops
cleanly, and so does closing the terminal window, logging out or shutting down:
the recording is finalised rather than left truncated. Paused time is cut out of
the finished file rather than appearing as a frozen frame.

The one way to lose a recording is killing the process outright (Task Manager's
End Task, `kill -9`); the operating system gives no program a chance to react to
that.

### Audio

System audio and the microphone are independent — record either, both or
neither. With both on they are mixed into a single track at their original
levels, so adding a microphone does not make the system audio quieter.

**Microphones are usually far quieter than system audio**, so a voice can be
buried under a game or video even though it is being recorded. Raise it with
`--mic-gain` (a multiplier: `2` is +6 dB, `4` is +12 dB) or set `mic_gain` in
the config.

If audio ends up slightly ahead of or behind the video on your machine, nudge it
with `--audio-offset 0.2`.

### Recording one application's audio

```bash
yefees-recorder record --app-audio "Firefox"
```

Only Linux can do this directly, by routing that application through a capture
sink with `pactl` and putting it back when the recording ends. On Windows and
macOS the operating system offers no way for ffmpeg to capture a single
application, so the command explains the alternative instead: send the app to
its own output device (Windows: Settings > System > Sound > Volume mixer;
macOS: a virtual device such as BlackHole) and record that device with
`--audio-device`.

`sources` lists every audio device separately as `audio` (what the machine is
playing) and `mic` (what it can hear). On a machine with several outputs —
a monitor's speakers and a headset, say — each one is its own `audio` entry,
so `--audio-device` picks which one is recorded.

### Presets

```bash
yefees-recorder record --save-preset gameplay --display 1 --fps 60 -q high
yefees-recorder record --preset gameplay
yefees-recorder config                # lists saved presets
```

Presets are appended to the config file as `[presets.NAME]` blocks, so you can
also write them by hand. A flag still overrides a preset.

### Shell completion

```bash
yefees-recorder --install-completion
```

## Platform notes

### macOS

Grant **Screen Recording** to your terminal in System Settings > Privacy &
Security, then restart it — macOS only applies the change to newly launched
processes. Without the grant avfoundation never delivers a single frame and
ffmpeg waits for one forever, so `doctor` checks the permission before anything
starts and `record` refuses rather than hanging.

**Audio needs Microphone permission too**, in System Settings > Privacy &
Security > Microphone, for the same terminal. macOS asks for it before handing
over *any* audio input, virtual devices like BlackHole included, and without it
the recording keeps the screen but has no sound — which the command says at the
time rather than leaving you to discover it later.

System audio needs a loopback device, because macOS has no way to record its own
output:

```bash
brew install blackhole-2ch
```

Then open **Audio MIDI Setup**, create a *Multi-Output Device* containing both
BlackHole and your speakers, and select it as the system output. Without the
multi-output you record the sound but stop hearing it. With no loopback device
at all you get video only, which the command says at the time.

**`--region` is in pixels, not points.** avfoundation captures at the display's
backing resolution, so on a Retina screen `--region 0,0,1280x720` covers the
640x360 points you actually see. Double the numbers you read off Screenshot.

`--window` is not available: avfoundation exposes whole screens and nothing
smaller. Use `--region` for the area a window occupies.

### Linux

X11 sessions use `x11grab` for video and the PulseAudio/PipeWire sink monitor
for system audio. `--window` needs `wmctrl` installed.

This path is **untested on real hardware** — see the table at the top.

### Wayland

ffmpeg cannot capture the screen there — access is only available through
xdg-desktop-portal/PipeWire — so `wf-recorder` is required. It supports wlroots
compositors (Sway, Hyprland, river). On GNOME or KDE, use your desktop's own
recorder or run an X11 session.

Wayland also asks *you* to pick the screen or window in its own dialog; that
choice is an operating-system security boundary and cannot be automated, so
`--display` and `--window` are unavailable there.

## Configuration

Settings come from three places, and the first one that has an answer wins:

**a flag on the command line** → **the config file** → **the built-in default**

So the config file holds what you want most of the time, and a flag overrides it
for one recording without changing anything.

```bash
yefees-recorder config          # where the file lives and what is in effect
yefees-recorder config --edit   # change settings from a menu, no flags needed
yefees-recorder config --init   # write a commented starter file
```

`config` prints every setting, its current value, and whether that value came
from the file or the default — which is the quickest way to find out why a
recording did something you did not ask for.

### Where the file lives

| | Path |
|---|---|
| Windows | `%LOCALAPPDATA%\yefees-recorder\config.toml` |
| macOS | `~/Library/Application Support/yefees-recorder/config.toml` |
| Linux | `~/.config/yefees-recorder/config.toml` |

Set `YEFEES_RECORDER_CONFIG` to a path to use a different file — useful for
keeping a separate profile, or for trying something without touching your real
settings:

```bash
YEFEES_RECORDER_CONFIG=./test.toml yefees-recorder config --edit
```

A broken or misspelled setting is reported as a warning and skipped; it never
stops a recording.

### The settings editor

```bash
yefees-recorder config --edit
```

opens an arrow-key menu — no flags, no editing TOML by hand, and nothing is
written until you choose **Save**.

| Key | What it does |
|---|---|
| **↑ ↓** | move between rows |
| **Enter**, **→** or **space** | open a page, or choose the highlighted value |
| **←** | go back one page (ignored on the top page) |
| **Esc**, **Backspace** or **q** | go back, and from the top page, quit |

On a slider:

| Key | What it does |
|---|---|
| **← →** | nudge the value |
| **↑ ↓** | bigger steps |
| **t** | type an exact value the steps cannot land on |
| **Enter** | accept |
| **Backspace** | leave it as it was |

Each row on the top page shows what it currently holds:

- **Video source** — whole desktop, a monitor, a window, or an area. This is one
  choice rather than three settings, because only one of them can be recorded:
  picking a window clears the monitor and the area automatically.
- **Audio** — system audio on/off and its device and level, the microphone
  on/off and its device and level, one application's audio, and the A/V offset.
- **Output and quality** — where recordings are saved, the frame rate, and the
  quality preset (`low` smallest files, `balanced` the default, `high` best
  looking).
- **Menu colour** — the accent the menus are built from.
- **Save and exit** / **Quit without saving** — the row tells you whether there
  is anything to save.

Device settings (monitor, window, audio device, microphone) offer what this
machine actually has as a list, so you pick a real device instead of typing its
name and hoping. Scanning happens once when a page first needs it, and each list
has a **Rescan** row for when you plug something in while the menu is open.

**Audio levels are edited in decibels and stored as multipliers.** dB is the
scale the numbers mean something on; ffmpeg wants the multiplier. So `+6 dB` in
the menu and `mic_gain = 2.0` in the file are the same thing. The slider turns
yellow past +6 dB and red past +14 dB, where clipping starts.

The menus are built from a single accent colour, indigo by default. The text
colour on a highlighted row is worked out from that colour's brightness, so a
pale accent gets black text and a dark one gets white — you cannot pick a colour
that makes the selection unreadable.

When you save, only the settings you changed are written, your comments and
layout survive, and the editor re-reads the file afterwards and shows you what
it actually holds rather than what it believes it wrote.

### Every setting

```toml
# Where recordings go. Unset means the current directory.
output_dir = "~/Videos"
fps = 30                        # capture frame rate
quality = "balanced"            # low | balanced | high
accent = "#5A4FCF"              # menu colour: a hex value or a name like "blue_violet"

audio = true                    # record what the machine plays
audio_device = "Speakers"       # unset = let the recorder choose
audio_gain = 1.0                # multiplier; 2.0 is +6 dB
mic = false                     # also record the microphone
mic_device = "Headset"
mic_gain = 4.0                  # mics need boosting more often than not
app_audio = "Firefox"           # one application's audio (Linux only)
audio_offset = 0.0              # seconds; nudge if audio drifts from the video

# Only one of these three may be set — they are mutually exclusive.
display = 0                     # monitor index, as `sources` prints it
window = "Firefox"              # window title
region = "0,0,1280x720"         # x,y,WIDTHxHEIGHT
```

| Setting | Type | Default | Notes |
|---|---|---|---|
| `output_dir` | string | current directory | `~` is expanded |
| `fps` | integer | `30` | |
| `quality` | string | `"balanced"` | `low`, `balanced` or `high` |
| `accent` | string | indigo `#5A4FCF` | hex, or a colour name |
| `audio` | boolean | `true` | system audio |
| `audio_device` | string | chosen for you | see `sources` |
| `audio_gain` | number | `1.0` | multiplier, not dB |
| `mic` | boolean | `false` | |
| `mic_device` | string | chosen for you | see `sources` |
| `mic_gain` | number | `1.0` | multiplier, not dB |
| `app_audio` | string | unset | Linux only |
| `audio_offset` | number | `0.0` | seconds |
| `display` | integer | unset | mutually exclusive with the two below |
| `window` | string | unset | |
| `region` | string | unset | `x,y,WIDTHxHEIGHT` |

Presets live in the same file and override the top-level values when selected:

```toml
[presets.gameplay]
display = 1
fps = 60
quality = "high"
mic = true
mic_gain = 4.0
```

## Development

```bash
uv sync
uv run yefees-recorder --help
uv run pytest
```

The test suite runs anywhere: the capture lifecycle is exercised against
ffmpeg's synthetic `lavfi` source rather than a real screen, and the tests that
genuinely need a platform skip themselves elsewhere. CI runs it on Linux (3.10
and 3.13), macOS and Windows.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

ffmpeg is invoked as an external program and is not bundled or linked, so its
own licensing is independent of this package's.

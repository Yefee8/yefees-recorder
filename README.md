# yefees-recorder

Cross-platform screen recorder CLI. Windows and Linux are implemented; macOS is not yet.

| Platform | Screen | System audio | Microphone |
|---|---|---|---|
| Windows | gdigrab | WASAPI loopback, no virtual cable needed | dshow |
| Linux / X11 | x11grab | PulseAudio / PipeWire sink monitor | PulseAudio source |
| Linux / Wayland | wf-recorder (wlroots only — not GNOME/KDE) | sink monitor | one source only |
| macOS | avfoundation screen capture | BlackHole or similar virtual device | avfoundation |

Only the Windows path has been tested on real hardware so far.

ffmpeg is not bundled; install it first:

```
winget install Gyan.FFmpeg     # Windows
brew install ffmpeg            # macOS
sudo apt install ffmpeg        # Debian/Ubuntu
```

## Usage

```
yefees-recorder doctor                  # check ffmpeg is installed
yefees-recorder sources                 # list displays, windows and audio devices
yefees-recorder record                  # record until Ctrl+C
yefees-recorder record -d 30 -o clip.mp4
yefees-recorder record --no-audio --fps 60
```

### Choosing what to record

`sources` prints the exact flag for each thing it finds:

```
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

### While recording

Press **p** (or space) to pause and resume, **q** to stop. Ctrl+C also stops
cleanly. Paused time is cut out of the finished file rather than appearing as a
frozen frame.

### Audio

System audio and the microphone are independent — record either, both or
neither. With both on they are mixed into a single track at their original
levels, so adding a microphone does not make the system audio quieter.

**Microphones are usually far quieter than system audio**, so a voice can be
buried under a game or video even though it is being recorded. Raise it with
`--mic-gain` (a multiplier: `2` is +6 dB, `4` is +12 dB) or set `mic_gain` in
the config.

### Recording one application's audio

```
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

```
yefees-recorder record --save-preset gameplay --display 1 --fps 60 -q high
yefees-recorder record --preset gameplay
yefees-recorder config                # lists saved presets
```

Presets are appended to the config file as `[presets.NAME]` blocks, so you can
also write them by hand. A flag still overrides a preset.

### Shell completion

```
yefees-recorder --install-completion
```

Recording the whole desktop is the default, which on a multi-monitor machine
means every monitor side by side — use `--display` for just one.

Window capture is not available everywhere: Windows does it natively, X11 needs
`wmctrl` installed, and macOS cannot do it at all (avfoundation only exposes
whole screens, so use `--region` there).

If audio ends up slightly ahead of or behind the video on your machine, nudge it
with `--audio-offset 0.2`.

On macOS you must grant Screen Recording permission to your terminal in System
Settings > Privacy & Security, then restart it — without it macOS records black
frames instead of reporting an error. System audio needs a loopback device
(`brew install blackhole-2ch`); without one you get video only.

On Wayland, ffmpeg cannot capture the screen — access is only available through
xdg-desktop-portal/PipeWire — so `wf-recorder` is required. It supports wlroots
compositors (Sway, Hyprland, river). On GNOME or KDE, use your desktop's own
recorder or run an X11 session.

## Development

```
uv sync
uv run yefees-recorder --help
uv run pytest
```

## Configuration

Flags always win; the config file only supplies what you leave out.

```
yefees-recorder config          # where it lives and what is in effect
yefees-recorder config --edit   # change settings from a menu, no flags needed
yefees-recorder config --init   # write a commented starter file
```

`config --edit` opens an arrow-key menu: move with up/down, open a submenu or
choose with Enter, go back with Left or Esc. Settings are grouped into **Video
source**, **Audio**, and **Output and quality**, and device settings offer what
this machine actually has, so you pick a monitor or a microphone from a list
instead of typing its name.

Video source is a single choice — whole desktop, a monitor, a window or an area
— because only one of them can be recorded. Picking a window clears the monitor
and the area automatically.

Audio levels are set in **decibels** on a slider: left and right nudge by
0.5 dB, up and down by 3 dB, and pressing **t** lets you type an exact value.
The bar turns yellow past +6 dB and red past +14 dB, where clipping starts.
Levels are stored as the multiplier ffmpeg needs, so `mic_gain = 2.0` in the
file and `+6 dB` in the menu are the same thing.

Move with the arrow keys, **Enter** to choose, **Left** or **Backspace** to go
back. Devices are scanned once when a page first needs them; pick **Rescan** in
a device list if you plug something in while the menu is open.

The menus are built around one colour, indigo by default. Change it under
**Menu colour**, or set it in the config:

```toml
accent = "#5A4FCF"    # a hex value, or a name like "blue_violet"
```

The text colour on a highlighted row is worked out from that colour's
brightness, so a pale accent gets black text and a dark one gets white.

Nothing is written until you choose Save, only the settings you changed are
written, and your comments survive.

```toml
output_dir = "~/Videos"
fps = 30
quality = "balanced"

audio = true                    # record what the machine plays
audio_device = "Speakers"
audio_gain = 1.0
mic = false                     # also record the microphone
mic_device = "Headset"
mic_gain = 4.0                  # mics need boosting more often than not
audio_offset = 0.0
app_audio = "Firefox"           # Linux only

display = 0
```

Set `YEFEES_RECORDER_CONFIG` to use a different file. A broken or misspelled
setting is reported and skipped rather than stopping the recording.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

ffmpeg is invoked as an external program and is not bundled or linked, so its
own licensing is independent of this package's.

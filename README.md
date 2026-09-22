# yefees-recorder

Cross-platform screen recorder CLI. Windows and Linux are implemented; macOS is not yet.

| Platform | Screen | System audio |
|---|---|---|
| Windows | gdigrab | WASAPI loopback, no virtual cable needed |
| Linux / X11 | x11grab | PulseAudio / PipeWire sink monitor |
| Linux / Wayland | wf-recorder (wlroots only — not GNOME/KDE) | sink monitor |
| macOS | avfoundation screen capture | BlackHole or similar virtual device |

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
yefees-recorder record -q high                       # low | balanced | high
yefees-recorder record --pick                        # choose from a menu instead
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
yefees-recorder config --init   # write a commented starter file
```

```toml
output_dir = "~/Videos"
fps = 30
quality = "balanced"
audio = true
display = 0
```

Set `YEFEES_RECORDER_CONFIG` to use a different file. A broken or misspelled
setting is reported and skipped rather than stopping the recording.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

ffmpeg is invoked as an external program and is not bundled or linked, so its
own licensing is independent of this package's.

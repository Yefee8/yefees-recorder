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
yefees-recorder record                  # record until Ctrl+C
yefees-recorder record -d 30 -o clip.mp4
yefees-recorder record --no-audio --fps 60
```

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

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

ffmpeg is invoked as an external program and is not bundled or linked, so its
own licensing is independent of this package's.

# yefees-recorder

Cross-platform screen recorder CLI. **Windows only so far** — Linux and macOS backends are not implemented yet.

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

On Windows the screen is captured with gdigrab and system audio with WASAPI
loopback — no virtual audio cable needed. If audio ends up slightly ahead of or
behind the video on your machine, nudge it with `--audio-offset 0.2`.

## Development

```
uv sync
uv run yefees-recorder --help
uv run pytest
```

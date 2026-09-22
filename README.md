# yefees-recorder

Cross-platform (Linux / macOS / Windows) screen recorder CLI, built on [mpv](https://mpv.io).

mpv and ffmpeg are **not** bundled — install them yourself, then check with:

```
yefees-recorder doctor
```

## Development

```
uv sync
uv run yefees-recorder --help
uv run pytest
```

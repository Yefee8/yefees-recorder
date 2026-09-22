"""Config file handling.

Precedence is flag, then config file, then built-in default — so a flag always
wins and the file only supplies what was left unsaid.

The file is only ever read here, never written from parsed data: `--init`
writes a fixed commented template, which avoids a TOML *writer* dependency and
keeps the user's own comments safe from being rewritten.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, NamedTuple

if sys.version_info >= (3, 11):
    import tomllib
else:  # tomllib only became stdlib in 3.11
    import tomli as tomllib

from platformdirs import user_config_dir

DEFAULTS: dict[str, Any] = {
    "output_dir": None,
    "fps": 30,
    "quality": "balanced",
    "audio": True,
    "audio_offset": 0.0,
    "audio_device": None,
    "display": None,
    "window": None,
    "region": None,
}

# bool before int on purpose: in Python bool is a subclass of int, so checking
# int first would let `fps = true` through.
TYPES: dict[str, Any] = {
    "output_dir": str,
    "fps": int,
    "quality": str,
    "audio": bool,
    "audio_offset": (int, float),
    "audio_device": str,
    "display": int,
    "window": str,
    "region": str,
}

TEMPLATE = """\
# yefees-recorder configuration.
# Anything set here is a default; a command-line flag always overrides it.
# Uncomment a line to use it.

# Where recordings are written when -o is not given.
# output_dir = "~/Videos"

# fps = 30
# quality = "balanced"   # low | balanced | high

# audio = true
# audio_device = "Speakers"   # substring of a name from `yefees-recorder sources`
# audio_offset = 0.0          # seconds; raise if audio runs early

# display = 0   # record one monitor by default
# window = "Firefox"
# region = "0,0,1280x720"

# Named sets of settings, used with: yefees-recorder record --preset gameplay
# Save the flags you just used with: record ... --save-preset gameplay
# [presets.gameplay]
# display = 1
# fps = 60
# quality = "high"
"""


class Loaded(NamedTuple):
    values: dict[str, Any]
    presets: dict[str, dict[str, Any]]
    warnings: list[str]


def _validate(data: dict[str, Any], where: str) -> tuple[dict[str, Any], list[str]]:
    values, warnings = {}, []
    for key, value in data.items():
        if key not in DEFAULTS:
            warnings.append(f"Unknown setting {key!r} in {where}")
        elif key == "audio" and not isinstance(value, bool):
            warnings.append(f"{key!r} should be true or false, not {value!r}")
        elif not isinstance(value, TYPES[key]) or (key != "audio" and isinstance(value, bool)):
            warnings.append(f"{key!r} has the wrong type in {where}: {value!r}")
        else:
            values[key] = value
    return values, warnings


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'



ENV_VAR = "YEFEES_RECORDER_CONFIG"


def config_path() -> Path:
    """The config file, overridable with $YEFEES_RECORDER_CONFIG."""
    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path(user_config_dir("yefees-recorder", appauthor=False)) / "config.toml"


def load(path: Path | None = None) -> Loaded:
    """Settings and presets from the config file, plus complaints about it.

    A broken config never stops a recording: bad values are dropped and
    reported, and the defaults take over.
    """
    path = path or config_path()
    if not path.exists():
        return Loaded({}, {}, [])
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return Loaded({}, {}, [f"Ignoring {path}: {exc}"])

    raw_presets = data.pop("presets", {})
    values, warnings = _validate(data, str(path))

    presets = {}
    if not isinstance(raw_presets, dict):
        warnings.append(f"'presets' should be a table of named settings in {path}")
    else:
        for name, body in raw_presets.items():
            if not isinstance(body, dict):
                warnings.append(f"Preset {name!r} should be a table, e.g. [presets.{name}]")
                continue
            presets[name], preset_warnings = _validate(body, f"preset {name!r}")
            warnings.extend(preset_warnings)
    return Loaded(values, presets, warnings)


def append_preset(name: str, values: dict[str, Any], path: Path | None = None) -> Path:
    """Add a `[presets.<name>]` block to the config file.

    Appends text rather than rewriting parsed data, so existing comments and
    formatting survive untouched. Refuses to shadow an existing preset, which
    duplicate TOML tables would anyway make a parse error.
    """
    path = path or config_path()
    if name in load(path).presets:
        raise ValueError(f"Preset {name!r} already exists in {path}; edit or remove it first.")
    if not values:
        raise ValueError("Nothing to save — pass the options you want the preset to remember.")

    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else TEMPLATE
    block = [f"\n[presets.{name}]"]
    block += [f"{key} = {_toml_scalar(value)}" for key, value in sorted(values.items())]
    body = "\n".join(block)
    path.write_text(existing.rstrip("\n") + "\n" + body + "\n", encoding="utf-8")
    return path


def resolve(key: str, flag_value: Any, values: dict[str, Any]) -> Any:
    """The flag if one was given, else the config file, else the default."""
    if flag_value is not None:
        return flag_value
    return values.get(key, DEFAULTS[key])


def write_template(path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")
    return path

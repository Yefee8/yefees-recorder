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
from typing import Any

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
"""


ENV_VAR = "YEFEES_RECORDER_CONFIG"


def config_path() -> Path:
    """The config file, overridable with $YEFEES_RECORDER_CONFIG."""
    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path(user_config_dir("yefees-recorder", appauthor=False)) / "config.toml"


def load(path: Path | None = None) -> tuple[dict[str, Any], list[str]]:
    """Settings from the config file, plus any complaints about its contents.

    A broken config never stops a recording: bad values are dropped and
    reported, and the defaults take over.
    """
    path = path or config_path()
    if not path.exists():
        return {}, []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return {}, [f"Ignoring {path}: {exc}"]

    values, warnings = {}, []
    for key, value in data.items():
        if key not in DEFAULTS:
            warnings.append(f"Unknown setting {key!r} in {path}")
        elif key == "audio" and not isinstance(value, bool):
            warnings.append(f"{key!r} should be true or false, not {value!r}")
        elif not isinstance(value, TYPES[key]) or (key != "audio" and isinstance(value, bool)):
            warnings.append(f"{key!r} has the wrong type in {path}: {value!r}")
        else:
            values[key] = value
    return values, warnings


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

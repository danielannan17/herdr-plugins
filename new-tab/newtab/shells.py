"""Finds, first-run creates and reads the user's shells.toml."""
from __future__ import annotations

import os
import subprocess

from . import logic

PLUGIN_ID = "danielannan17.new-tab"
EXAMPLE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shells.example.toml"
)
FILE_NAME = "shells.toml"


def _config_dir() -> str:
    """Herdr's per-plugin config folder: HERDR_PLUGIN_CONFIG_DIR, else asked of the CLI."""
    path = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if path:
        return path
    herdr_bin = os.environ.get("HERDR_BIN_PATH") or "herdr"
    plugin_id = os.environ.get("HERDR_PLUGIN_ID") or PLUGIN_ID
    result = subprocess.run(
        [herdr_bin, "plugin", "config-dir", plugin_id],
        capture_output=True, text=True, timeout=5, check=True,
    )
    path = result.stdout.strip()
    if not path:
        raise OSError("Herdr gave no config folder")
    return path


def _read(path: str) -> tuple[tuple[logic.Shell, ...], str | None]:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as err:
        return (logic.PLAIN_SHELL,), f"{FILE_NAME}: {getattr(err, 'strerror', None) or err}"
    return logic.parse_shells(text)


def _create(path: str) -> None:
    """Copy the example to `path`; mode "x" means an existing file is never overwritten."""
    with open(EXAMPLE_PATH, encoding="utf-8") as f:
        text = f.read()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "x", encoding="utf-8") as f:
        f.write(text)


def load() -> tuple[tuple[logic.Shell, ...], str | None]:
    """Shells to offer, plus an error message to show if the file could not be used.

    `NEW_TAB_SHELLS` names a file to read as is. Otherwise the file lives in the
    plugin's config folder and is created from the example on first use; if that
    fails, the example's own shells are used for this popup.
    """
    override = os.environ.get("NEW_TAB_SHELLS")
    if override:
        return _read(override)
    try:
        path = os.path.join(_config_dir(), FILE_NAME)
        if not os.path.exists(path):
            _create(path)
    except FileExistsError:
        pass
    except (OSError, UnicodeDecodeError, subprocess.SubprocessError) as err:
        shell_list, error = _read(EXAMPLE_PATH)
        reason = getattr(err, "strerror", None) or str(err)
        return shell_list, error or f"{FILE_NAME}: could not create it ({reason}); using the example"
    return _read(path)

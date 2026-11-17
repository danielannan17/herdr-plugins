"""Mode, configuration and the per-session Stop snapshots."""

import os
import time
from dataclasses import dataclass
from typing import Optional

from . import paths
from .fsutil import read_json, read_text, write_atomic, write_json

MODES = ("off", "auto", "forced")
NEXT_MODE = {"off": "auto", "auto": "forced", "forced": "off"}


@dataclass(frozen=True)
class Config:
    default_mode: str = "auto"
    release_grace_sec: float = 5.0
    unknown_grace_sec: float = 120.0
    unknown_fallback: str = "ignore"  # ignore | keep
    stuck_after_sec: float = 6 * 3600.0
    hard_cap_sec: Optional[float] = None
    snapshot_grace_sec: float = 5.0
    notify_mode_change: bool = True
    notify_stuck: bool = True
    tick_sec: float = 30.0
    herdr_gone_after: int = 3


def _num(table, key, default, scale=1.0):
    value = table.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value) * scale
    return default


def parse_config(table):
    base = Config()
    fallback = table.get("unknown_fallback", base.unknown_fallback)
    mode = table.get("default_mode", base.default_mode)
    hard_cap = table.get("hard_cap_hours")
    return Config(
        default_mode=mode if mode in MODES else base.default_mode,
        release_grace_sec=_num(table, "release_grace_sec", base.release_grace_sec),
        unknown_grace_sec=_num(table, "unknown_grace_sec", base.unknown_grace_sec),
        unknown_fallback=fallback if fallback in ("ignore", "keep") else base.unknown_fallback,
        stuck_after_sec=_num(table, "stuck_after_hours", base.stuck_after_sec, 3600.0),
        hard_cap_sec=_num(table, "hard_cap_hours", None, 3600.0) if hard_cap else None,
        snapshot_grace_sec=_num(table, "snapshot_grace_sec", base.snapshot_grace_sec),
        notify_mode_change=bool(table.get("notify_mode_change", base.notify_mode_change)),
        notify_stuck=bool(table.get("notify_stuck", base.notify_stuck)),
        tick_sec=max(5.0, _num(table, "tick_sec", base.tick_sec)),
    )


def load_config():
    path = os.path.join(paths.config_dir(), "config.toml")
    try:
        import tomllib

        with open(path, "rb") as fh:
            return parse_config(tomllib.load(fh))
    except (OSError, ImportError, ValueError):
        return Config()


def mode_path():
    return paths.in_state("mode")


def get_mode(config=None):
    value = read_text(mode_path())
    if value in MODES:
        return value
    return (config or load_config()).default_mode


def set_mode(mode):
    if mode not in MODES:
        raise ValueError("mode must be one of %s" % ", ".join(MODES))
    write_atomic(mode_path(), mode + "\n")
    return mode


def cycle_mode(config=None):
    return set_mode(NEXT_MODE[get_mode(config)])


def snapshot_path(session_id):
    safe = "".join(c if c.isalnum() or c in "_.-" else "_" for c in session_id or "none")
    return paths.in_state("snapshots", safe + ".json")


def write_snapshot(session_id, task_ids, now=None):
    write_json(
        snapshot_path(session_id),
        {"session_id": session_id, "ts": time.time() if now is None else now, "task_ids": sorted(task_ids)},
    )


def load_snapshots():
    out = {}
    directory = paths.in_state("snapshots")
    try:
        names = os.listdir(directory)
    except OSError:
        return out
    for name in names:
        snap = read_json(os.path.join(directory, name)) if name.endswith(".json") else None
        if isinstance(snap, dict) and snap.get("session_id"):
            out[snap["session_id"]] = snap
    return out


def remove_snapshot(session_id):
    try:
        os.unlink(snapshot_path(session_id))
    except OSError:
        pass

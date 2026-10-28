"""Turns (mode, config, current workspace) into something to run or a message to show."""
import os
import shutil
from dataclasses import dataclass, field

from .config import Config, ConfigError, match_workspace
from .screen import TITLES


@dataclass(frozen=True)
class Launch:
    argv: tuple
    env: dict
    cwd: str
    bare_keys: bool | None = None


@dataclass(frozen=True)
class Plan:
    kind: str  # run | message
    launch: Launch | None = None
    title: str = ""
    lines: tuple = ()
    reload_hint: bool = False  # show "r Reload" because editing config or installing a tool can fix it
    workspace: str | None = None

    def signature(self):
        if self.kind == "run":
            return ("run", self.launch.argv, self.launch.cwd, tuple(sorted(self.launch.env.items())))
        return ("message", self.title, self.lines)


def message(mode, lines, reload_hint=True):
    return Plan("message", title=TITLES[mode], lines=tuple(lines), reload_hint=reload_hint)


def _find_executable(argv0, env, cwd):
    if os.sep in argv0:
        path = argv0 if os.path.isabs(argv0) else os.path.join(cwd, argv0)
        return path if os.path.isfile(path) and os.access(path, os.X_OK) else None
    return shutil.which(argv0, path=env.get("PATH", os.defpath))


def resolve(mode, config, workspace, base_env, config_path):
    """config is a Config, or the ConfigError raised while loading it."""
    if isinstance(config, ConfigError):
        return message(mode, ["Invalid configuration.", str(config), f"File: {config_path}"], True)

    label = (workspace or {}).get("label")
    cwd_hint = (workspace or {}).get("cwd")
    entry = None
    if mode == "personal":
        dashboard = config.personal
        if dashboard is None:
            return message(mode, ["No personal dashboard configured yet."], False)
        default_cwd = os.path.expanduser("~")
    else:
        if workspace is None:
            return message(mode, ["Herdr did not say which workspace is current."], False)
        entry = match_workspace(config, label, cwd_hint)
        if entry is None:
            return message(mode, ["No dashboard configured for this workspace.", f"Workspace: {label}"], False)
        dashboard = entry.dashboard
        default_cwd = os.path.expanduser(entry.paths[0]) if entry.paths else (cwd_hint or os.path.expanduser("~"))

    cwd = os.path.expanduser(dashboard.cwd) if dashboard.cwd else default_cwd
    env = dict(base_env)
    env.update(dashboard.env)
    env.setdefault("TERM", "xterm-256color")
    env["HERDR_DASHBOARD_MODE"] = mode
    env["HERDR_DASHBOARD_WORKSPACE"] = label or ""
    argv = [os.path.expanduser(a) if a.startswith("~") else a for a in dashboard.argv]
    shown = " ".join(argv)

    if not os.path.isdir(cwd):
        return message(mode, ["Working directory not found.", f"cwd: {cwd}", f"Command: {shown}"], True)
    found = _find_executable(argv[0], env, cwd)
    if found is None:
        return message(mode, [f"Executable not found: {argv[0]}", f"Command: {shown}",
                               "Install it, or fix the command in config.toml."], True)
    argv[0] = found
    return Plan("run", launch=Launch(tuple(argv), env, cwd, dashboard.bare_keys), workspace=label)

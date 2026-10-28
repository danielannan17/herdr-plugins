"""config.toml parsing and workspace resolution."""
import os
import re
import shlex
import tomllib
from dataclasses import dataclass, field

MODES = ("personal", "work")
DASHBOARD_KEYS = {"command", "args", "env", "cwd", "bare_keys"}
ENTRY_KEYS = DASHBOARD_KEYS | {"label", "path"}
TOP_KEYS = {"personal", "workspaces", "keys", "popup", "ui"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Dashboard:
    argv: tuple
    env: dict = field(default_factory=dict)
    cwd: str | None = None
    bare_keys: bool | None = None


@dataclass(frozen=True)
class Entry:
    key: str
    labels: tuple
    paths: tuple
    dashboard: Dashboard


@dataclass(frozen=True)
class Config:
    personal: Dashboard | None = None
    workspaces: tuple = ()
    leader: str | None = None
    toggle: str | None = None
    bare_keys: bool = False
    width: object = "90%"
    height: object = "90%"
    footer: bool = True


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _str_list(value, where):
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(value)
    raise ConfigError(f"{where} must be a string or a list of strings")


def _dashboard(table, where):
    unknown = set(table) - ENTRY_KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
    command = table.get("command")
    if command is None:
        return None
    if isinstance(command, str):
        try:
            argv = shlex.split(command)
        except ValueError as e:
            raise ConfigError(f"{where}.command: {e}")
    elif isinstance(command, list) and all(isinstance(v, str) for v in command):
        argv = list(command)
    else:
        raise ConfigError(f"{where}.command must be a string or a list of strings")
    if not argv:
        raise ConfigError(f"{where}.command is empty")
    argv += list(_str_list(table.get("args", []), f"{where}.args"))
    env = table.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        raise ConfigError(f"{where}.env must be a table of strings")
    cwd = table.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ConfigError(f"{where}.cwd must be a string")
    bare = table.get("bare_keys")
    if bare is not None and not isinstance(bare, bool):
        raise ConfigError(f"{where}.bare_keys must be true or false")
    return Dashboard(tuple(argv), dict(env), cwd, bare)


def _size(value, where):
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"(100|[1-9][0-9]?)%", value):
        return value
    raise ConfigError(f'{where} must be a cell count or a percentage like "80%"')


def parse(text):
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"TOML syntax error: {e}")
    unknown = set(data) - TOP_KEYS
    if unknown:
        raise ConfigError(f"unknown section(s) {', '.join(sorted(unknown))}")

    personal = None
    if "personal" in data:
        if not isinstance(data["personal"], dict):
            raise ConfigError("[personal] must be a table")
        personal = _dashboard(data["personal"], "[personal]")

    entries = []
    workspaces = data.get("workspaces", {})
    if not isinstance(workspaces, dict):
        raise ConfigError("[workspaces] must be a table of tables")
    for key, table in workspaces.items():
        where = f"[workspaces.{key}]"
        if not isinstance(table, dict):
            raise ConfigError(f"{where} must be a table")
        dashboard = _dashboard(table, where)
        if dashboard is None:
            continue
        labels = _str_list(table.get("label", key), f"{where}.label")
        paths = _str_list(table.get("path", []), f"{where}.path")
        entries.append(Entry(key, labels, paths, dashboard))

    kw = {}
    keys = data.get("keys", {})
    if set(keys) - {"leader", "toggle", "bare"}:
        raise ConfigError("[keys]: only leader, toggle and bare are known")
    for name in ("leader", "toggle"):
        if name in keys:
            if not isinstance(keys[name], str):
                raise ConfigError(f"[keys].{name} must be a string")
            kw[name] = keys[name]
    if "bare" in keys:
        if not isinstance(keys["bare"], bool):
            raise ConfigError("[keys].bare must be true or false")
        kw["bare_keys"] = keys["bare"]
    popup = data.get("popup", {})
    if set(popup) - {"width", "height"}:
        raise ConfigError("[popup]: only width and height are known")
    for name in ("width", "height"):
        if name in popup:
            kw[name] = _size(popup[name], f"[popup].{name}")
    ui = data.get("ui", {})
    if set(ui) - {"footer"}:
        raise ConfigError("[ui]: only footer is known")
    if "footer" in ui:
        if not isinstance(ui["footer"], bool):
            raise ConfigError("[ui].footer must be true or false")
        kw["footer"] = ui["footer"]
    return Config(personal=personal, workspaces=tuple(entries), **kw)


def load(path):
    """A missing file is an empty config; an unreadable or invalid one raises."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return Config()
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"cannot read {path}: {e}")
    return parse(text)


def _within(path, root):
    path, root = os.path.realpath(path), os.path.realpath(os.path.expanduser(root))
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def match_workspace(config, label, cwd):
    """The entry for a workspace: label first (normalised), then the longest matching path."""
    wanted = slug(label or "")
    for entry in config.workspaces:
        if wanted and any(slug(l) == wanted for l in entry.labels):
            return entry
    if cwd:
        best, best_len = None, -1
        for entry in config.workspaces:
            for root in entry.paths:
                length = len(os.path.realpath(os.path.expanduser(root)))
                if _within(cwd, root) and length > best_len:
                    best, best_len = entry, length
        return best
    return None


def herdr_prefix_binding(path, plugin_action="herdr-dashboard.open"):
    """(prefix, key) from Herdr's own config.toml: its prefix and the key bound to our action."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None, None
    prefix = data.get("keys", {}).get("prefix")
    prefix = prefix[0] if isinstance(prefix, list) and prefix else prefix
    key = None
    for binding in data.get("keys", {}).get("command", []):
        if binding.get("command") == plugin_action and str(binding.get("key", "")).startswith("prefix+"):
            key = binding["key"][len("prefix+"):]
    return (prefix if isinstance(prefix, str) else None), key

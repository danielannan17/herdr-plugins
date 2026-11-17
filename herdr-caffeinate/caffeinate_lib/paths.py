"""Where the plugin keeps its files.

herdr hands plugin commands HERDR_PLUGIN_STATE_DIR and HERDR_PLUGIN_CONFIG_DIR.
The CLI and the Claude Code mod run outside herdr and get neither, so both fall
back to herdr's own layout, which is the same directory herdr would have passed.
"""

import os

PLUGIN_ID = "danielannan17.caffeinate"
SOURCE = "caffeinate"  # metadata source id and token name


def state_dir():
    path = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if path:
        return path
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "herdr", "plugins", PLUGIN_ID)


def config_dir():
    path = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if path:
        return path
    return os.path.expanduser("~/.config/herdr/plugins/config/%s" % PLUGIN_ID)


def plugin_root():
    return os.environ.get("HERDR_PLUGIN_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )


def socket_path():
    return os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser(
        "~/.config/herdr/herdr.sock"
    )


def herdr_bin():
    return os.environ.get("HERDR_BIN_PATH") or "herdr"


def in_state(*parts):
    return os.path.join(state_dir(), *parts)

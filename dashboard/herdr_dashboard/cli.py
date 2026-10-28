"""Commands: `open` toggles the popup; `run` is the popup's own process."""
import os
import subprocess
import sys
import time

from . import herdr_api
from .config import Config, ConfigError, load
from .state import State

PLUGIN_ID = "herdr-dashboard"


def _is_our_popup(pid):
    try:
        os.kill(pid, 0)
        command = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                 capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "dashboard.py" in command and " run" in command


def _wait_gone(pid, seconds=4.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and _is_our_popup(pid):
        time.sleep(0.05)


def open_action(env=None):
    env = os.environ if env is None else env
    state = State(env["HERDR_PLUGIN_STATE_DIR"])
    here = herdr_api.context().get("tab_id")
    pid, tab = state.popup()
    if pid and _is_our_popup(pid):
        herdr_api.close_popup()
        if not (tab and here) or tab == here:
            return 0  # visible in this tab: the shortcut toggles it closed
        _wait_gone(pid)  # a dashboard left open on another tab is replaced by one here
    try:
        config = load(os.path.join(env["HERDR_PLUGIN_CONFIG_DIR"], "config.toml"))
    except ConfigError:
        config = Config()  # the popup itself shows the error
    try:
        herdr_api.open_popup(env.get("HERDR_PLUGIN_ID", PLUGIN_ID), "dashboard", config.width, config.height, {})
    except herdr_api.ApiError as e:
        if e.code == "ui_busy":
            herdr_api.notify("Another popup or dialog is open; close it first.")
        print(f"herdr-dashboard: could not open the popup: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv):
    if argv[:1] == ["open"]:
        return open_action()
    if argv[:1] == ["run"]:
        from .supervisor import main as run_popup
        return run_popup()
    print("usage: dashboard.py open|run", file=sys.stderr)
    return 2

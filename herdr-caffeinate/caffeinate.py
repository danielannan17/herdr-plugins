#!/usr/bin/env python3
"""Entry points herdr calls: start, poke, actions, the details popup, the daemon.

  caffeinate.py start            start the daemon if it is not running
  caffeinate.py poke             start it if needed, then make it re-evaluate
  caffeinate.py action NAME      cycle | off | auto | forced
  caffeinate.py open-details     open the details popup
  caffeinate.py details-popup    what the popup runs
  caffeinate.py --daemon         run the daemon in the foreground
"""

import os
import select
import subprocess
import sys
import termios
import tty

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from caffeinate_lib import control, display, engine, fsutil, herdr, paths, state  # noqa: E402

LABEL = {"off": "Off", "auto": "Auto", "forced": "Forced"}
HINT = {
    "off": "never keeps the Mac awake",
    "auto": "awake while agents work or finite background tasks run",
    "forced": "always awake",
}


def action(name):
    cfg = state.load_config()
    if name == "cycle":
        mode = state.cycle_mode(cfg)
    else:
        mode = state.set_mode(name)
    control.ensure_daemon()
    control.poke()
    if cfg.notify_mode_change:
        herdr.notify("Caffeinate: %s" % LABEL[mode], HINT[mode])
    return 0


def open_details():
    return subprocess.call([
        paths.herdr_bin(), "plugin", "pane", "open", "--plugin", paths.PLUGIN_ID,
        "--entrypoint", "details", "--placement", "popup",
    ])


def details_popup():
    """Print the details view, refresh it every two seconds, close on any key."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            view = engine.snapshot()
            holding = display.is_holding(fsutil.read_text(paths.in_state("status.txt")))
            sys.stdout.write("\x1b[2J\x1b[H" + display.details(view, holding).replace("\n", "\r\n"))
            sys.stdout.write("\r\n\r\n(any key closes)\r\n")
            sys.stdout.flush()
            if select.select([sys.stdin], [], [], 2.0)[0]:
                return 0
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def main(argv):
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == "--daemon":
        from caffeinate_lib import daemon
        return daemon.serve()
    if cmd == "start":
        control.ensure_daemon()
        return 0
    if cmd == "poke":
        control.ensure_daemon()
        control.poke()
        return 0
    if cmd == "action" and len(argv) == 2 and argv[1] in ("cycle", "off", "auto", "forced"):
        return action(argv[1])
    if cmd == "open-details":
        return open_details()
    if cmd == "details-popup":
        return details_popup()
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

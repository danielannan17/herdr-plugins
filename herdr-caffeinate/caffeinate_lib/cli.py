"""The `herdr-caffeinate` command line."""

import argparse
import sys

from . import control, display, engine, fsutil, paths, policy, registry, rules, state


def _poke():
    control.ensure_daemon()
    control.poke()


def _status_file():
    return fsutil.read_text(paths.in_state("status.txt"))


def _status_text():
    text = _status_file()
    if text and control.daemon_running():
        return text
    view = engine.snapshot()
    return policy.summary(view.decision, False, view.pairs) + "  (daemon not running)"


def show_details():
    view = engine.snapshot()
    holding = control.daemon_running() and display.is_holding(_status_file())
    print(display.details(view, holding))


def _task_ref(args):
    return (args.session or "", args.task_id)


def cmd_register(args):
    cls, label = args.cls, args.label
    if cls is None:
        cls, label = rules.classify(args.command or "", args.description or "")
    keep = True if args.keep_awake is None else args.keep_awake
    task = registry.register(
        args.task_id, args.session or "", args.command or "", args.description or "", cls,
        keep, source="cli", kind="manual", pid=args.pid, timeout=args.timeout, label=label,
    )
    _poke()
    print("registered %s (%s)" % (task["task_id"], task["class"]))
    return 0


def cmd_update(args):
    changes = {"source": "cli"}
    if args.cls:
        changes["class"] = args.cls
    if args.keep_awake is not None:
        changes["keep_awake"] = args.keep_awake
    for name in ("pid", "timeout", "description", "command", "label"):
        if getattr(args, name, None) is not None:
            changes[name] = getattr(args, name)
    task = registry.update(args.session or "", args.task_id, **changes)
    if task is None:
        print("no such task", file=sys.stderr)
        return 1
    _poke()
    return 0


def cmd_complete(args):
    status = args.status
    task = registry.complete(args.session or "", args.task_id, status)
    if task is None:
        print("no such task", file=sys.stderr)
        return 1
    _poke()
    return 0


def cmd_override(args):
    matches = registry.find(args.task)
    if not matches:
        print("no task matches %r (use TASK_ID or SESSION:TASK_ID)" % args.task, file=sys.stderr)
        return 1
    if len(matches) > 1:
        print("%r is ambiguous; use SESSION:TASK_ID. Candidates:" % args.task, file=sys.stderr)
        for t in matches:
            print("  %s:%s" % (t.get("session_id"), t["task_id"]), file=sys.stderr)
        return 1
    value = None if args.value == "clear" else args.value
    registry.set_override(matches[0].get("session_id", ""), matches[0]["task_id"], value)
    _poke()
    return 0


def cmd_mode(args):
    if args.value is None:
        print(state.get_mode())
        return 0
    mode = state.cycle_mode() if args.value == "cycle" else state.set_mode(args.value)
    _poke()
    print(mode)
    return 0


def cmd_status(_args):
    print(_status_text())
    return 0


def cmd_details(_args):
    show_details()
    return 0


def cmd_poke(_args):
    _poke()
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="herdr-caffeinate", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="one-line state").set_defaults(fn=cmd_status)
    sub.add_parser("details", help="active, ignored, unclassified and stuck work").set_defaults(fn=cmd_details)
    sub.add_parser("poke", help="start the daemon if needed and make it re-evaluate").set_defaults(fn=cmd_poke)

    m = sub.add_parser("mode", help="show or set the mode")
    m.add_argument("value", nargs="?", choices=("off", "auto", "forced", "cycle"))
    m.set_defaults(fn=cmd_mode)

    def task_args(sp, full):
        sp.add_argument("--task-id", required=True)
        sp.add_argument("--session", default="")
        if full:
            sp.add_argument("--command", default="")
            sp.add_argument("--description", default="")
            sp.add_argument("--class", dest="cls", choices=("finite", "persistent", "unknown"))
            sp.add_argument("--label")
            sp.add_argument("--pid", type=int)
            sp.add_argument("--timeout", type=float, help="seconds after which the task expires")
            sp.add_argument("--keep-awake", dest="keep_awake", action="store_true", default=None)
            sp.add_argument("--no-keep-awake", dest="keep_awake", action="store_false")

    r = sub.add_parser("register", help="register a background task by hand")
    task_args(r, True)
    r.set_defaults(fn=cmd_register)
    u = sub.add_parser("update", help="change a registered task")
    task_args(u, True)
    u.set_defaults(fn=cmd_update)
    c = sub.add_parser("complete", help="mark a task finished")
    task_args(c, False)
    c.add_argument("--status", default="completed", choices=("completed", "failed", "killed"))
    c.set_defaults(fn=cmd_complete)

    o = sub.add_parser("override", help="force a task to keep awake, or be ignored")
    o.add_argument("task", help="TASK_ID or SESSION:TASK_ID")
    o.add_argument("value", choices=("keep", "ignore", "clear"))
    o.set_defaults(fn=cmd_override)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.fn(args)

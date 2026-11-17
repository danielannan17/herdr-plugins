"""Background tasks, one JSON file each, in <state dir>/tasks/.

Writers are the Claude Code mod, the CLI and the daemon (which only deletes and
stamps the stuck notification), so each write is a whole-file atomic replace.
A task is identified by (session_id, task_id): two sessions running the same
command hold two tasks.
"""

import os
import re
import time

from . import paths
from .fsutil import read_json, write_json

# Who set a task's class. A lower-ranked source never overwrites a higher one:
# an agent's own statement beats a pattern rule, and the user beats everyone.
SOURCE_RANK = {"rule": 0, "fork": 1, "agent": 2, "cli": 3, "user": 3}

STATUS_RUNNING = "running"

FIELDS = (
    "task_id", "session_id", "kind", "command", "description", "class", "keep_awake",
    "source", "pid", "started_at", "updated_at", "status", "override", "label",
    "timeout", "snapshot_tracked", "stuck_notified_at",
)


def tasks_dir():
    return paths.in_state("tasks")


def _safe(part):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", part or "none")


def task_path(session_id, task_id):
    return os.path.join(tasks_dir(), "%s__%s.json" % (_safe(session_id), _safe(task_id)))


def load_all():
    """Every readable task. Unreadable files are skipped for this pass."""
    out = []
    try:
        names = sorted(os.listdir(tasks_dir()))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        task = read_json(os.path.join(tasks_dir(), name))
        if isinstance(task, dict) and task.get("task_id"):
            out.append(task)
    return out


def get(session_id, task_id):
    task = read_json(task_path(session_id, task_id))
    return task if isinstance(task, dict) else None


def register(task_id, session_id="", command="", description="", cls="unknown",
             keep_awake=True, source="cli", kind="manual", pid=None, timeout=None,
             label=None, snapshot_tracked=False, now=None):
    now = time.time() if now is None else now
    task = {
        "task_id": task_id,
        "session_id": session_id or "",
        "kind": kind,
        "command": command,
        "description": description,
        "class": cls,
        "keep_awake": bool(keep_awake),
        "source": source,
        "pid": pid,
        "started_at": now,
        "updated_at": now,
        "status": STATUS_RUNNING,
        "override": None,
        "label": label,
        "timeout": timeout,
        "snapshot_tracked": snapshot_tracked,
    }
    existing = get(session_id, task_id)
    if existing:
        # Registering a known task again refreshes it and keeps what is already settled.
        task["started_at"] = existing.get("started_at", now)
        task["override"] = existing.get("override")
        if SOURCE_RANK.get(existing.get("source"), 0) > SOURCE_RANK.get(source, 0):
            for key in ("class", "keep_awake", "source", "label"):
                task[key] = existing.get(key, task[key])
    write_json(task_path(session_id, task_id), task)
    return task


def update(session_id, task_id, now=None, **changes):
    """Merge `changes` into a task. Returns the task, or None when it is unknown.

    A class change from a lower-ranked source than the one that set the current
    class is dropped, so a pattern rule cannot undo what the agent said.
    """
    task = get(session_id, task_id)
    if task is None:
        return None
    source = changes.get("source", task.get("source", "rule"))
    outranked = SOURCE_RANK.get(source, 0) < SOURCE_RANK.get(task.get("source"), 0)
    for key, value in changes.items():
        if key not in FIELDS or value is None and key not in ("override", "pid", "timeout"):
            continue
        if outranked and key in ("class", "keep_awake", "source", "label"):
            continue
        task[key] = value
    task["updated_at"] = time.time() if now is None else now
    write_json(task_path(session_id, task_id), task)
    return task


def complete(session_id, task_id, status="completed", now=None):
    return update(session_id, task_id, now=now, status=status)


def set_override(session_id, task_id, override):
    if override not in ("keep", "ignore", None):
        raise ValueError("override must be keep, ignore or clear")
    return update(session_id, task_id, override=override)


def remove(task):
    try:
        os.unlink(task_path(task.get("session_id", ""), task["task_id"]))
    except OSError:
        pass


def find(ref):
    """Tasks matching a CLI reference: `task_id` or `session_id:task_id`."""
    session, _, tid = ref.rpartition(":")
    return [
        t for t in load_all()
        if t["task_id"] == tid and (not session or t.get("session_id") == session)
    ]

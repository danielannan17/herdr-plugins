"""Which registered tasks are still alive. Pure: the caller supplies the facts.

A task is dropped when its completion was reported, its recorded pid is dead,
its owning agent session is gone with no live pid to say otherwise, its
session's latest Stop snapshot no longer lists it, or a configured limit passed.
A long task is never dropped for duration alone.
"""

from .registry import STATUS_RUNNING


def reconcile(tasks, now, cfg, pid_alive, agent_sessions=None, snapshots=None):
    """Split `tasks` into (alive, dropped) where dropped is [(task, reason)].

    agent_sessions: set of session ids herdr currently lists, or None when the
    list could not be read (the session check is then skipped, never guessed).
    """
    snapshots = snapshots or {}
    alive, dropped = [], []
    for task in tasks:
        reason = _drop_reason(task, now, cfg, pid_alive, agent_sessions, snapshots)
        if reason:
            dropped.append((task, reason))
        else:
            alive.append(task)
    return alive, dropped


def _drop_reason(task, now, cfg, pid_alive, agent_sessions, snapshots):
    status = task.get("status") or STATUS_RUNNING
    if status != STATUS_RUNNING:
        return "reported %s" % status

    started = float(task.get("started_at") or now)
    pid = task.get("pid")
    pid_dead = pid is not None and not pid_alive(pid)
    timeout = task.get("timeout")

    if timeout and now > started + float(timeout):
        return "passed its %ds timeout" % int(timeout)
    if pid_dead:
        return "process %s exited" % pid
    if cfg.hard_cap_sec and task.get("override") != "keep" and now - started > cfg.hard_cap_sec:
        return "passed the %dh hard cap" % int(cfg.hard_cap_sec // 3600)

    session = task.get("session_id")
    if session and agent_sessions is not None and session not in agent_sessions and pid is None:
        return "its agent session is gone"

    snap = snapshots.get(session) if session else None
    if (
        snap
        and task.get("snapshot_tracked")
        and started + cfg.snapshot_grace_sec < float(snap.get("ts") or 0)
        and task["task_id"] not in set(snap.get("task_ids") or [])
    ):
        return "no longer in its agent's task list"
    return None

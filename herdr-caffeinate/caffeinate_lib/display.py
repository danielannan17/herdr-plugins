"""Text for the details view and the CLI."""

from . import policy
from .policy import ACTIVE, IGNORED, UNCLASSIFIED


def is_holding(status_text):
    """Whether an indicator text from status.txt says the Mac is being held awake."""
    return "Awake" in status_text or "Forced" in status_text


def _age(seconds):
    seconds = int(seconds)
    if seconds < 90:
        return "%ds" % seconds
    if seconds < 5400:
        return "%dm" % (seconds // 60)
    return "%dh%02dm" % (seconds // 3600, seconds % 3600 // 60)


def _task_line(task, verdict, now):
    text = (task.get("description") or task.get("command") or task["task_id"]).strip().splitlines()[0]
    ref = "%s:%s" % (task.get("session_id") or "-", task["task_id"])
    return "  %s  %s  [%s, %s]\n      %s" % (
        text[:70], ref[:50], task.get("kind", "?"), _age(policy.task_age(task, now)), verdict.reason,
    )


def details(view, holding):
    """The Active / Ignored / Unclassified / Possibly stuck sections."""
    now = view.now
    lines = ["herdr-caffeinate  mode: %s  %s" % (view.mode, "HOLDING AWAKE" if holding else "not holding")]
    if not view.agents_known:
        lines.append("(herdr could not be read; agents unknown)")
    lines.append("")
    stuck = [(t, v) for t, v in view.pairs if v.stuck]
    stuck_ids = {id(t) for t, _ in stuck}

    lines.append("Agents working (%d)" % len(view.decision.working_agents))
    for a in view.decision.working_agents:
        lines.append("  %s  %s  [%s]" % (a.get("name") or a.get("agent") or "agent", a.get("pane_id", ""), "working"))
    waiting = [a for a in view.agents if a.get("agent_status") == "blocked"]
    if waiting:
        lines.append("  (%d agent%s waiting on you; does not hold the Mac awake)"
                     % (len(waiting), "" if len(waiting) == 1 else "s"))

    sections = (
        ("Active", [(t, v) for t, v in view.pairs if v.bucket == ACTIVE and id(t) not in stuck_ids]),
        ("Possibly stuck", stuck),
        ("Unclassified", [(t, v) for t, v in view.pairs if v.bucket == UNCLASSIFIED]),
        ("Ignored", [(t, v) for t, v in view.pairs if v.bucket == IGNORED]),
    )
    for title, rows in sections:
        lines.append("")
        lines.append("%s (%d)" % (title, len(rows)))
        lines += [_task_line(t, v, now) for t, v in rows] or ["  none"]
    return "\n".join(lines)

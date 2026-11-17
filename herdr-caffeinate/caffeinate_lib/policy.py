"""What should keep the Mac awake. Pure: no IO, no clock, no processes.

Verdicts say whether one background task counts. `decide` combines agents and
tasks into one yes or no for the current mode. `Grace` turns that into the hold
the daemon applies, releasing a few seconds after the last activity.
"""

from dataclasses import dataclass


ACTIVE = "active"
IGNORED = "ignored"
UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class Verdict:
    counts: bool
    bucket: str  # active | ignored | unclassified
    reason: str
    stuck: bool = False
    unknown_expires: float = 0.0  # epoch seconds the unknown grace ends, 0 when n/a


def task_age(task, now):
    return max(0.0, now - float(task.get("started_at") or now))


def verdict(task, now, cfg):
    """Whether `task` keeps the Mac awake, and why. User overrides beat everything."""
    override = task.get("override")
    cls = task.get("class") or "unknown"
    age = task_age(task, now)
    expires = 0.0
    stuck_ok = True

    if override == "keep":
        counts, bucket, reason = True, ACTIVE, "kept awake by your override"
        stuck_ok = False
    elif override == "ignore":
        counts, bucket, reason = False, IGNORED, "ignored by your override"
    elif cls == "persistent":
        counts, bucket = False, IGNORED
        reason = "persistent service (%s)" % _by(task)
    elif cls == "finite":
        if task.get("keep_awake") is False:
            counts, bucket, reason = False, IGNORED, "finite, but %s said it need not keep awake" % _by(task)
        else:
            counts, bucket, reason = True, ACTIVE, "finite work (%s)" % _by(task)
    else:
        expires = float(task.get("started_at") or now) + cfg.unknown_grace_sec
        if age < cfg.unknown_grace_sec:
            counts, bucket = True, UNCLASSIFIED
            reason = "unclassified, counted for %ds more" % int(cfg.unknown_grace_sec - age)
        elif cfg.unknown_fallback == "keep":
            counts, bucket, reason = True, UNCLASSIFIED, "unclassified, fallback is keep awake"
        else:
            counts, bucket, reason = False, UNCLASSIFIED, "unclassified, fallback is ignore"

    stuck = counts and stuck_ok and age > cfg.stuck_after_sec
    return Verdict(counts, bucket, reason, stuck, expires)


def _by(task):
    return {"rule": "by pattern rule", "agent": "by its agent", "fork": "by asking its agent",
            "cli": "by CLI", "user": "by you"}.get(task.get("source"), task.get("source") or "?")


@dataclass(frozen=True)
class Decision:
    wanted: bool
    mode: str
    working_agents: tuple
    active_tasks: tuple


def decide(mode, agents, tasks_with_verdicts):
    """`wanted` is the instant answer, before the release grace.

    Only a `working` agent counts: `blocked` (waiting for you), `idle`, `done`
    and `unknown` never keep the Mac awake by themselves.
    """
    working = tuple(a for a in agents if a.get("agent_status") == "working")
    active = tuple(t for t, v in tasks_with_verdicts if v.counts)
    if mode == "off":
        wanted = False
    elif mode == "forced":
        wanted = True
    else:
        wanted = bool(working or active)
    return Decision(wanted, mode, working, active)


class Grace:
    """Hold while wanted, and for `seconds` after it stops being wanted."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.last_wanted = None  # epoch seconds, None before the first wanted

    def hold(self, mode, wanted, now):
        if mode == "off":
            self.last_wanted = None
            return False
        if wanted:
            self.last_wanted = now
            return True
        return self.last_wanted is not None and now - self.last_wanted < self.seconds

    def release_at(self):
        return None if self.last_wanted is None else self.last_wanted + self.seconds


def next_wakeup(now, tick, grace_release_at, verdicts):
    """Seconds until the earliest moment the answer can change without an event."""
    waits = [tick]
    if grace_release_at is not None and grace_release_at > now:
        waits.append(grace_release_at - now)
    for v in verdicts:
        if v.counts and v.unknown_expires > now:
            waits.append(v.unknown_expires - now)
    return max(0.2, min(waits))


def summary(decision, holding, verdict_pairs):
    """The indicator text: `☕ Awake · 2 agents · 1 build`."""
    if decision.mode == "off":
        return "☕ Off"
    stuck = sum(1 for _, v in verdict_pairs if v.stuck)
    suffix = " · ⚠ %d stuck" % stuck if stuck else ""
    if decision.mode == "forced":
        return "☕ Forced" + suffix
    if not holding:
        return "☕ Auto · Idle" + suffix
    parts = []
    n = len(decision.working_agents)
    if n:
        parts.append("%d agent%s" % (n, "" if n == 1 else "s"))
    counts = {}
    for task in decision.active_tasks:
        label = task.get("label") or ("subagent" if task.get("kind") == "subagent" else "task")
        counts[label] = counts.get(label, 0) + 1
    for label in sorted(counts):
        parts.append("%d %s" % (counts[label], label))
    if not parts:
        parts.append("releasing")
    return "☕ Awake · " + " · ".join(parts) + suffix

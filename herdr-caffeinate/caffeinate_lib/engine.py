"""One evaluation: read the world, apply the policy, return what to show.

`snapshot` does no writes. The daemon deletes the tasks it lists as dropped;
the CLI only displays them.
"""

import os
import time
from dataclasses import dataclass

from . import herdr, liveness, policy, registry, state


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError, OverflowError):
        return False
    return True


@dataclass
class View:
    now: float
    mode: str
    cfg: state.Config
    agents: list
    agents_known: bool
    pairs: list  # [(task, Verdict)] of tasks still alive
    dropped: list  # [(task, reason)]
    decision: policy.Decision = None


_READ = object()


def snapshot(now=None, cfg=None, mode=None, agents=_READ):
    """`agents` is the list to use, None when herdr could not be read; left out, it is read now."""
    now = time.time() if now is None else now
    cfg = cfg or state.load_config()
    mode = mode or state.get_mode(cfg)
    if agents is _READ:
        agents = herdr.list_agents()
    sessions = None
    if agents is not None:
        sessions = {
            (a.get("agent_session") or {}).get("value")
            for a in agents if (a.get("agent_session") or {}).get("value")
        }
    alive, dropped = liveness.reconcile(
        registry.load_all(), now, cfg, pid_alive, sessions, state.load_snapshots()
    )
    pairs = [(t, policy.verdict(t, now, cfg)) for t in alive]
    view = View(now, mode, cfg, agents or [], agents is not None, pairs, dropped)
    view.decision = policy.decide(mode, view.agents, pairs)
    return view

"""The singleton that holds `caffeinate -i` while important work runs.

Event driven: herdr's event hooks poke a FIFO, and the loop sleeps until a poke
or the next moment the answer could change by itself (the release grace, an
unknown task's grace). A slow tick re-checks task liveness, and a cheap probe of
herdr's socket every few seconds ends the daemon, and with it the hold, when
herdr is gone.
"""

import os
import select
import signal
import subprocess
import sys
import time

from . import control, engine, herdr, paths, policy, registry, state
from .fsutil import write_atomic

LIVENESS_SEC = 10.0
INDICATOR_REFRESH_SEC = 60.0
INDICATOR_TTL_MS = 150_000  # the sidebar token fades by itself if the daemon dies
SNAPSHOT_KEEP_SEC = 24 * 3600
AGENTS_STALE_SEC = 60.0


def log(message):
    try:
        with open(paths.in_state("daemon.log"), "a", encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (time.strftime("%F %T"), message))
    except OSError:
        pass


class Holder:
    """Owns the single `caffeinate -i -w <our pid>` child.

    `-w` ties the assertion to this process, so the kernel releases it when the
    daemon dies by any means, kill -9 included.
    """

    def __init__(self):
        self.proc = None

    @property
    def held(self):
        return self.proc is not None and self.proc.poll() is None

    def hold(self):
        if self.held:
            return
        self.proc = subprocess.Popen(
            ["caffeinate", "-i", "-w", str(os.getpid())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        log("holding awake (caffeinate pid %d)" % self.proc.pid)

    def release(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            log("released")
        self.proc = None


class Daemon:
    def __init__(self):
        self.cfg = state.load_config()
        self.grace = policy.Grace(self.cfg.release_grace_sec)
        self.holder = Holder()
        self.workspaces = []
        self.shown = {"text": None, "at": 0.0, "workspaces": ()}
        self.last_agents = None
        self.last_agents_at = 0.0

    def agents(self, now):
        """The agent list, or the last good one for a minute when herdr hiccups."""
        agents = herdr.list_agents()
        if agents is not None:
            self.last_agents, self.last_agents_at = agents, now
            return agents
        if self.last_agents is not None and now - self.last_agents_at < AGENTS_STALE_SEC:
            return self.last_agents
        return None

    def evaluate(self, now):
        self.cfg = state.load_config()
        self.grace.seconds = self.cfg.release_grace_sec
        mode = state.get_mode(self.cfg)
        view = engine.snapshot(now, self.cfg, mode, agents=self.agents(now))

        for task, reason in view.dropped:
            log("dropped task %s (%s): %s" % (task["task_id"], task.get("session_id", "")[:8], reason))
            registry.remove(task)
        self._prune_snapshots(view, now)

        holding = self.grace.hold(mode, view.decision.wanted, now)
        if holding:
            self.holder.hold()
        else:
            self.holder.release()
        text = policy.summary(view.decision, self.holder.held, view.pairs)
        self._show(text, now)
        self._notify_stuck(view, now)
        write_atomic(paths.in_state("status.txt"), text + "\n")
        return view, holding

    def _notify_stuck(self, view, now):
        for task, verdict in view.pairs:
            if verdict.stuck and not task.get("stuck_notified_at") and self.cfg.notify_stuck:
                hours = policy.task_age(task, now) / 3600
                herdr.notify(
                    "Possibly stuck: still keeping the Mac awake",
                    "%s (%.0fh). Ignore it with: herdr-caffeinate override %s ignore"
                    % ((task.get("description") or task.get("command") or task["task_id"])[:80],
                       hours, task["task_id"]),
                )
                registry.update(task.get("session_id", ""), task["task_id"], stuck_notified_at=now)

    def _prune_snapshots(self, view, now):
        if not view.agents_known:
            return
        live = {(a.get("agent_session") or {}).get("value") for a in view.agents}
        for sid, snap in state.load_snapshots().items():
            if sid not in live and now - float(snap.get("ts") or 0) > SNAPSHOT_KEEP_SEC:
                state.remove_snapshot(sid)

    def _show(self, text, now):
        ids = herdr.list_workspace_ids()
        if ids is None:
            return
        ids = tuple(ids)
        stale = now - self.shown["at"] >= INDICATOR_REFRESH_SEC
        if text == self.shown["text"] and ids == self.shown["workspaces"] and not stale:
            return
        for ws in ids:
            herdr.report_token(ws, text, INDICATOR_TTL_MS)
        self.shown = {"text": text, "at": now, "workspaces": ids}

    def shutdown(self):
        self.holder.release()
        for ws in self.shown["workspaces"]:
            try:
                herdr.clear_token(ws)
            except (OSError, subprocess.SubprocessError):
                pass
        log("daemon exiting")


def _open_fifo():
    path = control.fifo_path()
    try:
        os.unlink(path)
    except OSError:
        pass
    os.mkfifo(path, 0o600)
    reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    keepalive = os.open(path, os.O_WRONLY)  # no EOF storm when the last poker leaves
    return reader, keepalive


def serve():
    lock = control.acquire_lock()
    if lock is None:
        return 0
    try:
        open(paths.in_state("daemon.log"), "w").close()
    except OSError:
        pass
    log("daemon started (pid %d)" % os.getpid())
    write_atomic(paths.in_state("cli-path"),
                 os.path.join(paths.plugin_root(), "bin", "herdr-caffeinate") + "\n")
    reader, keepalive = _open_fifo()

    stopping = []

    def stop(*_):
        stopping.append(1)
        try:
            os.write(keepalive, b"x")  # wakes the select below
        except OSError:
            pass

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop)

    daemon = Daemon()
    failures = 0
    next_eval = 0.0
    next_liveness = 0.0
    try:
        while not stopping:
            now = time.time()
            if now >= next_liveness:
                next_liveness = now + LIVENESS_SEC
                failures = 0 if herdr.socket_reachable() else failures + 1
                if failures >= daemon.cfg.herdr_gone_after:
                    log("herdr unreachable %d times; exiting" % failures)
                    break
            if now >= next_eval:
                try:
                    view, _ = daemon.evaluate(now)
                    wait = policy.next_wakeup(
                        time.time(), daemon.cfg.tick_sec, daemon.grace.release_at(),
                        [v for _, v in view.pairs],
                    )
                except Exception as exc:  # noqa: BLE001 - one bad pass must not end the hold logic
                    log("evaluation failed: %r" % (exc,))
                    wait = daemon.cfg.tick_sec
                next_eval = time.time() + wait
            timeout = max(0.1, min(next_eval, next_liveness) - time.time())
            ready, _, _ = select.select([reader], [], [], timeout)
            if ready:
                try:
                    os.read(reader, 4096)
                except OSError:
                    pass
                next_eval = 0.0
    finally:
        daemon.shutdown()
        for fd in (reader, keepalive):
            os.close(fd)
        try:
            os.unlink(control.fifo_path())
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(serve())

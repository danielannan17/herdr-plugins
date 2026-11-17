"""Calls into the running herdr server, through its CLI."""

import json
import socket
import subprocess

from . import paths

TIMEOUT = 5


def _run(args):
    return subprocess.run(
        [paths.herdr_bin()] + list(args),
        capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL,
    )


def _json(args):
    """Parsed `result` of a herdr CLI call, or None when it failed."""
    try:
        proc = _run(args)
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout).get("result", {})
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def list_agents():
    """Every agent herdr lists, or None when herdr could not be read."""
    result = _json(["agent", "list"])
    if result is None:
        return None
    return [a for a in result.get("agents", []) if isinstance(a, dict)]


def list_workspace_ids():
    result = _json(["workspace", "list"])
    if result is None:
        return None
    return [w["workspace_id"] for w in result.get("workspaces", []) if w.get("workspace_id")]


def socket_reachable():
    """Whether anything accepts connections on herdr's socket. Cheap, no request."""
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(2)
    try:
        s.connect(paths.socket_path())
        return True
    except OSError:
        return False
    finally:
        s.close()


def report_token(workspace_id, text, ttl_ms):
    """Show `text` as the workspace's `caffeinate` token; it expires after ttl_ms."""
    # The workspace id has to come first: herdr's argument parser reads the first
    # bare word as the id, which would make it the value of `--source`.
    _run(["workspace", "report-metadata", workspace_id, "--source", paths.SOURCE, "--token",
          "%s=%s" % (paths.SOURCE, text), "--ttl-ms", str(int(ttl_ms))])


def clear_token(workspace_id):
    _run(["workspace", "report-metadata", workspace_id, "--source", paths.SOURCE,
          "--clear-token", paths.SOURCE])


def notify(title, body=""):
    # Title first, for the same parser reason as report_token.
    args = ["notification", "show", title, "--sound", "none"]
    if body:
        args += ["--body", body]
    try:
        _run(args)
    except (OSError, subprocess.SubprocessError):
        pass

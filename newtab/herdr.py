"""Calls into the running Herdr over its API socket (newline-delimited JSON).

The socket is used instead of the CLI because the CLI's argument parser cannot
carry every tab name (a name starting with "-" is read as an option).
"""
from __future__ import annotations

import itertools
import json
import os
import socket

from .logic import Workspace

TIMEOUT = 5.0
_ids = itertools.count(1)


class HerdrError(Exception):
    pass


def _socket_path() -> str:
    path = os.environ.get("HERDR_SOCKET_PATH")
    if not path:
        raise HerdrError("HERDR_SOCKET_PATH is not set")
    return path


def call(method: str, params: dict | None = None) -> dict:
    request = {"id": f"new-tab:{next(_ids)}", "method": method, "params": params or {}}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(TIMEOUT)
            sock.connect(_socket_path())
            sock.sendall((json.dumps(request) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except socket.timeout:
        raise HerdrError("Herdr did not respond")
    except OSError as err:
        raise HerdrError(err.strerror or str(err))
    try:
        data = json.loads(buf)
    except ValueError:
        raise HerdrError("unreadable response from Herdr")
    error = data.get("error") if isinstance(data, dict) else None
    if error:
        raise HerdrError(str(error.get("message") or error.get("code") or "unknown error"))
    return data.get("result", {})


def list_workspaces() -> list[Workspace]:
    result = call("workspace.list")
    return [
        Workspace(
            id=w["workspace_id"],
            label=w.get("label") or w["workspace_id"],
            number=int(w.get("number", 0)),
            focused=bool(w.get("focused")),
            tab_count=int(w.get("tab_count", 0)),
        )
        for w in result.get("workspaces", [])
    ]


def create_tab(workspace_id: str, label: str | None) -> None:
    """Create and focus a tab. Never sends cwd or env: Herdr resolves the
    working directory from the destination workspace's focused pane."""
    params = {"workspace_id": workspace_id, "focus": True}
    if label is not None:
        params["label"] = label
    call("tab.create", params)


def context_workspace_id() -> str | None:
    try:
        ctx = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", ""))
    except ValueError:
        return None
    ws = ctx.get("workspace_id") if isinstance(ctx, dict) else None
    return ws if isinstance(ws, str) else None

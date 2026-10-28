"""Talks to the running Herdr server over its socket (newline-delimited JSON)."""
import json
import os
import socket


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


def context():
    """The invocation context Herdr injects (workspace id/label/cwd, focused pane)."""
    try:
        data = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", ""))
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _connect(timeout):
    path = os.environ.get("HERDR_SOCKET_PATH")
    if not path:
        raise ApiError("no_socket", "HERDR_SOCKET_PATH is not set")
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
    except OSError as e:
        sock.close()
        raise ApiError("connect_failed", str(e))
    return sock


def request(method, params=None, timeout=3.0):
    sock = _connect(timeout)
    try:
        sock.sendall((json.dumps({"id": "hd", "method": method, "params": params or {}}) + "\n").encode())
        line = sock.makefile("r", encoding="utf-8").readline()
    except OSError as e:
        raise ApiError("io_failed", str(e))
    finally:
        sock.close()
    try:
        reply = json.loads(line)
    except ValueError:
        raise ApiError("bad_reply", line[:200])
    if "error" in reply:
        err = reply["error"]
        raise ApiError(err.get("code", "error"), err.get("message", ""))
    return reply.get("result", {})


def current_workspace():
    """{'id','label','cwd','tab'} of the focused tab's workspace, or None when Herdr cannot say."""
    try:
        snap = request("session.snapshot")["snapshot"]
    except (ApiError, KeyError):
        return None
    ws_id = snap.get("focused_workspace_id")
    ws = next((w for w in snap.get("workspaces", []) if w.get("workspace_id") == ws_id), None)
    if not ws:
        return None
    pane_id = next((l.get("focused_pane_id") for l in snap.get("layouts", [])
                    if l.get("tab_id") == snap.get("focused_tab_id")), None)
    pane = next((p for p in snap.get("panes", []) if p.get("pane_id") == pane_id), None)
    return {"id": ws_id, "label": ws.get("label"), "cwd": (pane or {}).get("cwd"),
            "tab": snap.get("focused_tab_id")}


def workspace_from_context(ctx):
    if not ctx.get("workspace_id"):
        return None
    return {"id": ctx["workspace_id"], "label": ctx.get("workspace_label"),
            "cwd": ctx.get("focused_pane_cwd") or ctx.get("workspace_cwd"), "tab": ctx.get("tab_id")}


def subscribe_workspaces():
    """A connected socket that yields a line whenever the focused tab or a workspace changes."""
    sock = _connect(3.0)
    types = ["workspace.focused", "workspace.renamed", "workspace.closed", "tab.focused", "tab.closed"]
    sock.sendall((json.dumps({"id": "sub", "method": "events.subscribe",
                              "params": {"subscriptions": [{"type": t} for t in types]}}) + "\n").encode())
    sock.settimeout(None)
    return sock


def open_popup(plugin_id, entrypoint, width, height, env):
    return request("plugin.pane.open", {
        "plugin_id": plugin_id, "entrypoint": entrypoint, "placement": "popup",
        "width": width, "height": height, "env": env, "focus": True})


def close_popup():
    return request("popup.close")


def notify(message):
    try:
        request("notification.show", {"title": "Dashboard", "body": message, "sound": "none"})
    except ApiError:
        pass

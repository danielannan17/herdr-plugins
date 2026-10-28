"""Pure state machine for the New Tab popup. No I/O.

`update` maps a key to a new state plus at most one effect for the caller to
perform. The caller reports what happened through `on_listing`,
`on_create_ok`, `on_create_failed` and `on_list_failed`.
"""
from __future__ import annotations

from collections import namedtuple

try:
    import tomllib
except ImportError:  # Python before 3.11
    tomllib = None

MAX_NAME = 64

EDITING = "editing"
CREATING = "creating"
# The tab exists but the shell command did not start; only closing is offered,
# so Enter cannot create a second tab.
CREATED = "created"
CLOSED = "closed"

EFFECT_SUBMIT = "submit"
EFFECT_CANCEL = "cancel"


Workspace = namedtuple("Workspace", "id label number focused tab_count")
Create = namedtuple("Create", "workspace_id label command", defaults=(None,))
# `command` is None for a plain shell.
Shell = namedtuple("Shell", "name command")
PLAIN_SHELL = Shell(name="shell", command=None)
# Immutable; derive new values with `_replace`.
State = namedtuple(
    "State",
    "workspaces dest_id name cursor dropdown highlight phase error shells shell_index",
    defaults=("", 0, False, 0, "editing", None, (PLAIN_SHELL,), 0),
)


def parse_shells(text: str) -> tuple[tuple[Shell, ...], str | None]:
    """Shells from shells.toml text. Falls back to one plain shell and an error message."""
    fallback = (PLAIN_SHELL,)
    if tomllib is None:
        return fallback, "shells.toml needs Python 3.11 or newer"
    try:
        entries = tomllib.loads(text).get("shell", [])
    except tomllib.TOMLDecodeError as err:
        return fallback, f"shells.toml: {err}"
    if not isinstance(entries, list) or not entries:
        return fallback, "shells.toml: no [[shell]] entries"
    shells = []
    for i, entry in enumerate(entries, 1):
        name = entry.get("name") if isinstance(entry, dict) else None
        command = entry.get("command") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name.strip():
            return fallback, f"shells.toml: shell {i} needs a name"
        if command is not None and not isinstance(command, str):
            return fallback, f"shells.toml: shell {i} command must be text"
        shells.append(Shell(name=name.strip(), command=(command or "").strip() or None))
    return tuple(shells), None


def initial_state(
    workspaces: list[Workspace],
    context_workspace_id: str | None = None,
    shells: tuple[Shell, ...] = (PLAIN_SHELL,),
) -> State:
    """Destination starts as the focused workspace, never a remembered one."""
    wss = tuple(workspaces)
    dest = next((w.id for w in wss if w.focused), None)
    if dest is None and context_workspace_id and any(w.id == context_workspace_id for w in wss):
        dest = context_workspace_id
    if dest is None and wss:
        dest = wss[0].id
    return State(workspaces=wss, dest_id=dest, shells=tuple(shells))


def find(workspaces: list[Workspace], workspace_id: str | None) -> Workspace | None:
    for ws in workspaces:
        if ws.id == workspace_id:
            return ws
    return None


def index_of(workspaces: list[Workspace], workspace_id: str | None) -> int:
    for i, ws in enumerate(workspaces):
        if ws.id == workspace_id:
            return i
    return -1


def default_name(ws: Workspace) -> str:
    """Name Herdr's own dialog pre-fills: tabs in the workspace plus one."""
    return str(ws.tab_count + 1)


def label_for(name: str, ws: Workspace) -> str | None:
    """Label to send with tab.create; None leaves naming to Herdr."""
    trimmed = name.strip()
    if not trimmed or trimmed == default_name(ws):
        return None
    return trimmed


def destination(state: State) -> Workspace | None:
    return find(state.workspaces, state.dest_id)


def current_shell(state: State) -> Shell:
    return state.shells[state.shell_index % len(state.shells)]


def active_workspace(state: State) -> Workspace | None:
    for ws in state.workspaces:
        if ws.focused:
            return ws
    return None


def _insert(state: State, text: str) -> State:
    room = MAX_NAME - len(state.name)
    if room <= 0:
        return state
    text = text[:room]
    name = state.name[: state.cursor] + text + state.name[state.cursor:]
    return state._replace(name=name, cursor=state.cursor + len(text), error=None)


def _edit(state: State, key: str) -> State:
    name, cur = state.name, state.cursor
    if key == "backspace":
        if cur == 0:
            return state
        return state._replace(name=name[: cur - 1] + name[cur:], cursor=cur - 1, error=None)
    if key == "delete":
        if cur >= len(name):
            return state
        return state._replace(name=name[:cur] + name[cur + 1:], error=None)
    if key in ("left",):
        return state._replace(cursor=max(0, cur - 1))
    if key in ("right",):
        return state._replace(cursor=min(len(name), cur + 1))
    if key in ("home", "ctrl+a"):
        return state._replace(cursor=0)
    if key in ("end", "ctrl+e"):
        return state._replace(cursor=len(name))
    if key == "ctrl+u":
        return state._replace(name=name[cur:], cursor=0, error=None)
    if key == "ctrl+w":
        head = name[:cur].rstrip()
        head = head[: head.rfind(" ") + 1]
        return state._replace(name=head + name[cur:], cursor=len(head), error=None)
    if len(key) == 1 and key.isprintable():
        return _insert(state, key)
    return state


def open_dropdown(state: State) -> State:
    idx = index_of(state.workspaces, state.dest_id)
    if idx < 0:
        idx = max(0, index_of(state.workspaces, getattr(active_workspace(state), "id", None)))
    return state._replace(dropdown=True, highlight=idx)


def select(state: State, index: int) -> State:
    ws = state.workspaces[index]
    return state._replace(dest_id=ws.id, dropdown=False, highlight=index, error=None)


def _dropdown_key(state: State, key: str) -> tuple[State, str | None]:
    n = len(state.workspaces)
    if key == "esc":
        return state._replace(dropdown=False), None
    if key == "ctrl+c":
        return state, EFFECT_CANCEL
    if n == 0:
        return state, None
    if key == "up":
        return state._replace(highlight=(state.highlight - 1) % n), None
    if key == "down":
        return state._replace(highlight=(state.highlight + 1) % n), None
    if key == "home":
        return state._replace(highlight=0), None
    if key == "end":
        return state._replace(highlight=n - 1), None
    if key == "enter":
        return select(state, min(state.highlight, n - 1)), None
    if len(key) == 1 and key in "123456789":
        idx = int(key) - 1
        if idx < n:
            return select(state, idx), None
    return state, None


def update(state: State, key: str) -> tuple[State, str | None]:
    """Apply one key. Returns (state, effect)."""
    if state.phase == CREATED:
        return (state, EFFECT_CANCEL) if key in ("enter", "esc", "ctrl+c") else (state, None)
    if state.phase != EDITING:
        return state, None
    if state.dropdown:
        return _dropdown_key(state, key)
    if key in ("esc", "ctrl+c"):
        return state, EFFECT_CANCEL
    if key == "enter":
        if state.dest_id is None or destination(state) is None:
            return state._replace(error="Pick a workspace first (Alt+Down)"), None
        return state._replace(phase=CREATING, error=None), EFFECT_SUBMIT
    if key in ("alt+down", "tab"):
        return (open_dropdown(state) if state.workspaces else state), None
    if key == "shift+tab":
        return state._replace(shell_index=(state.shell_index + 1) % len(state.shells)), None
    return _edit(state, key), None


def on_listing(state: State, fresh: list[Workspace]) -> tuple[State, Create | None]:
    """Resolve the destination against a fresh workspace listing at submit time."""
    fresh = tuple(fresh)
    dest = find(fresh, state.dest_id)
    if dest is None:
        gone = find(state.workspaces, state.dest_id)
        who = f"'{gone.label}'" if gone else "The selected workspace"
        noun = "was closed" if gone else "no longer exists"
        return (
            state._replace(
                
                workspaces=fresh,
                dest_id=None,
                phase=EDITING,
                dropdown=False,
                highlight=0,
                error=f"{who} {noun}. Pick a workspace (Alt+Down).",
            ),
            None,
        )
    return (
        state._replace(workspaces=fresh),
        Create(workspace_id=dest.id, label=label_for(state.name, dest), command=current_shell(state).command),
    )


def on_create_ok(state: State) -> State:
    return state._replace(phase=CLOSED, error=None)


def on_create_failed(state: State, message: str) -> State:
    return state._replace(phase=EDITING, error=f"Could not create tab: {message}")


def on_run_failed(state: State, message: str) -> State:
    shell = current_shell(state)
    return state._replace(
        phase=CREATED,
        error=f"Tab created, but '{shell.command}' did not start: {message}",
    )


def on_list_failed(state: State, message: str) -> State:
    return state._replace(phase=EDITING, error=f"Could not read workspaces: {message}")

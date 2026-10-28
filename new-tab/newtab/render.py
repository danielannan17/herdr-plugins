"""Pure rendering: State and a size to a list of terminal rows (SGR included).
The surrounding border and title are drawn by Herdr's popup frame."""
from __future__ import annotations

import re
import textwrap
import unicodedata

from .logic import CREATED, CREATING, State, active_workspace, current_shell, default_name, destination

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
REVERSE = "\x1b[7m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"

_SGR = re.compile(r"\x1b\[[0-9;]*m")

HINT = "Enter to create · Alt+↓ workspace · Shift+Tab shell"
CREATED_HINT = "Enter or Esc to close"
DROPDOWN_HINT_PARTS = ["1-9 select", "↑↓ move", "Enter choose", "Esc close"]
CHECK = "✓"
CARET_CLOSED = "⌄"
CARET_OPEN = "⌃"


def strip_sgr(text: str) -> str:
    return _SGR.sub("", text)


def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def text_width(text: str) -> int:
    return sum(char_width(c) for c in strip_sgr(text))


def fit(text: str, width: int) -> str:
    """Truncate plain text to `width` cells, with an ellipsis."""
    if width <= 0:
        return ""
    if text_width(text) <= width:
        return text
    out, used = "", 0
    for ch in text:
        w = char_width(ch)
        if used + w > width - 1:
            break
        out += ch
        used += w
    return out + "…"


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - text_width(text))


def _workspace_row(state: State, cols: int) -> str:
    ws = destination(state)
    label = ws.label if ws else "(none)"
    caret = CARET_OPEN if state.dropdown else CARET_CLOSED
    prefix = "Workspace: "
    tail = f"[{caret}]"
    room = cols - 2 - text_width(prefix) - text_width(tail) - 2
    shown = fit(label, room)
    gap = max(1, cols - 2 - text_width(prefix) - text_width(shown) - text_width(tail))
    style = BOLD if ws else DIM
    return f" {prefix}{style}{shown}{RESET}{' ' * gap}{DIM}{tail}{RESET}"


def _name_row(state: State, cols: int) -> str:
    prefix = "Tab name: "
    ws = destination(state)
    hint = f"default: {default_name(ws)}" if ws and not state.name else ""
    avail = cols - 2 - text_width(prefix) - (text_width(hint) + 2 if hint else 0)
    avail = max(avail, 3)
    name, cur = state.name, state.cursor
    start = 0
    if cur >= avail:
        start = cur - avail + 1
    window = name[start:start + avail]
    rel = cur - start
    before, at, after = window[:rel], window[rel:rel + 1], window[rel + 1:]
    caret = f"{REVERSE}{at or ' '}{RESET}"
    body = f"{before}{caret}{after}"
    used = text_width(prefix) + text_width(before) + max(1, text_width(at)) + text_width(after)
    right = ""
    if hint:
        gap = max(2, cols - 2 - used - text_width(hint))
        right = f"{' ' * gap}{DIM}{hint}{RESET}"
    return f" {prefix}{body}{right}"


def _shell_row(state: State, cols: int) -> str:
    prefix = "Shell: "
    shell = current_shell(state)
    shown = fit(shell.name, cols - 2 - text_width(prefix))
    return f" {prefix}{BOLD}{shown}{RESET}"


def window_start(highlight: int, total: int, visible: int) -> int:
    if total <= visible:
        return 0
    return max(0, min(highlight - visible // 2, total - visible))


def _dropdown_rows(state: State, cols: int, visible: int) -> list[str]:
    total = len(state.workspaces)
    start = window_start(state.highlight, total, visible)
    active = active_workspace(state)
    rows: list[str] = []
    for i in range(start, min(total, start + visible)):
        ws = state.workspaces[i]
        mark = CHECK if ws.id == state.dest_id else " "
        suffix = " · active" if active and ws.id == active.id else ""
        number = str(i + 1).rjust(2)
        label_room = cols - 2 - 6 - text_width(suffix)
        text = f" {mark} {number}  {fit(ws.label, label_room)}"
        if i == state.highlight:
            line = f"{REVERSE}{pad(text + suffix, cols)}{RESET}"
        else:
            body = f"{GREEN if mark != ' ' else ''}{text}{RESET}" if mark != " " else text
            line = f"{body}{DIM}{suffix}{RESET}" if suffix else body
        rows.append(line)
    return rows


def _dropdown_hint(above: int, below: int, cols: int) -> str:
    """Hidden-item counts lead the hint; trailing key hints drop until it fits."""
    parts = []
    if above:
        parts.append(f"↑ {above} more")
    if below:
        parts.append(f"↓ {below} more")
    parts.extend(DROPDOWN_HINT_PARTS)
    while len(parts) > 1 and text_width(" · ".join(parts)) > cols - 2:
        parts.pop()
    return fit(" · ".join(parts), cols - 2)


def render(state: State, rows: int, cols: int) -> list[str]:
    """Return exactly `rows` lines (at least 8).

    Row 0 is padding and row 1 the workspace row in both modes. Closed: the
    shell row, the name row, a blank, up to two error lines, then the hint on
    the last row. Open: the list fills every row between the workspace row and
    the hint."""
    rows = max(rows, 8)
    out: list[str] = [""] * rows
    out[1] = _workspace_row(state, cols)
    if state.dropdown:
        visible = rows - 3
        for k, line in enumerate(_dropdown_rows(state, cols, visible)):
            out[2 + k] = line
        total = len(state.workspaces)
        start = window_start(state.highlight, total, visible)
        hint = _dropdown_hint(start, max(0, total - (start + visible)), cols)
        out[rows - 1] = f" {DIM}{hint}{RESET}"
        return out
    out[2] = _shell_row(state, cols)
    out[3] = _name_row(state, cols)
    if state.error:
        wrapped = textwrap.wrap(state.error, max(10, cols - 2)) or [state.error]
        lines = wrapped[:2]
        if len(wrapped) > 2:
            lines[-1] = fit(lines[-1] + "…", cols - 2)
        first = rows - 1 - len(lines)
        for k, line in enumerate(lines):
            out[first + k] = f" {RED}{line}{RESET}"
    status = {CREATING: "Creating…", CREATED: CREATED_HINT}.get(state.phase, HINT)
    out[rows - 1] = f" {DIM}{fit(status, cols - 2)}{RESET}"
    return out

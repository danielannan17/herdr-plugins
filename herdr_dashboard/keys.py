"""Classifies terminal input bytes into forwarded bytes and dashboard commands.

Herdr's prefix keys never fire while a popup has focus (the popup is
session-modal and gets every byte), so the popup process re-implements the
prefix itself: leader then a command key. Bare Tab / Shift+Tab / Esc are only
intercepted when `bare` is on, because dashboards use them.
"""
import re
from typing import NamedTuple

ESC_TIMEOUT = 0.04
LEADER_TIMEOUT = 2.0
PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"
MODIFY_OTHER_KEYS = re.compile(rb"\x1b\[27;(\d+);(\d+)~")
CSI_U = re.compile(rb"\x1b\[(\d+)(?::\d*)*(?:;(\d*)(?::(\d+))?)?(?:;[\d:]*)?u")


class Action(NamedTuple):
    kind: str  # forward | close | next | prev | personal | work | retry
    data: bytes = b""


def leader_byte(spec):
    """'ctrl+b' -> b'\\x02'."""
    spec = spec.strip().lower()
    if spec.startswith("ctrl+") and len(spec) == 6 and spec[-1].isalpha():
        return bytes([ord(spec[-1]) - 96])
    raise ValueError(f"unsupported leader {spec!r}; use ctrl+<letter>")


def decode(token):
    """Name of a key token, or (name, True) for a key release; None when unnamed."""
    if token == b"\t":
        return "tab"
    if token == b"\x1b[Z":
        return "shift+tab"
    if token == b"\x1b":
        return "esc"
    if token in (b"\r", b"\n"):
        return "enter"
    if len(token) == 1:
        if token[0] < 27 and token[0] not in (9, 10, 13):
            return "ctrl+" + chr(token[0] + 96)
        if 32 <= token[0] < 127:
            return chr(token[0])
        return None
    m = CSI_U.fullmatch(token)
    if m:
        return _named(int(m.group(1)), int(m.group(2) or 1) - 1, m.group(3) == b"3")
    m = MODIFY_OTHER_KEYS.fullmatch(token)
    if m:
        return _named(int(m.group(2)), int(m.group(1)) - 1, False)
    return None


def _named(code, mods, release):
    """Key name for a code point and modifier bits (shift=1, alt=2, ctrl=4), as CSI-u and modifyOtherKeys send them."""
    mods = max(mods, 0)
    if code == 9:
        name = "shift+tab" if mods & 1 else "tab"
    elif code == 27:
        name = "esc"
    elif code in (13, 10):
        name = "enter"
    elif 32 <= code < 127 and mods & 4 and chr(code).isalpha():
        name = "ctrl+" + chr(code).lower()
    elif 32 <= code < 127 and not mods:
        name = chr(code)
    else:
        return None
    return (name, True) if release else name


def split(buf, final):
    """(tokens, rest): rest is an incomplete tail kept until more bytes or a timeout."""
    tokens, i, n = [], 0, len(buf)
    while i < n:
        if buf[i] != 0x1B:
            tokens.append(buf[i:i + 1])
            i += 1
            continue
        if i + 1 >= n:
            if final:
                tokens.append(buf[i:])
                i = n
            break
        nxt = buf[i + 1]
        if nxt == 0x5B:  # CSI
            j = i + 2
            while j < n and 0x30 <= buf[j] <= 0x3F:
                j += 1
            while j < n and 0x20 <= buf[j] <= 0x2F:
                j += 1
            if j >= n:
                if final:
                    tokens.append(buf[i:i + 1])
                    i += 1
                    continue
                break
            tokens.append(buf[i:j + 1])
            i = j + 1
        elif nxt == 0x4F:  # SS3
            if i + 3 > n:
                if final:
                    tokens.append(buf[i:i + 1])
                    i += 1
                    continue
                break
            tokens.append(buf[i:i + 3])
            i += 3
        elif nxt == 0x1B:  # Esc, then the start of another sequence
            tokens.append(buf[i:i + 1])
            i += 1
        else:  # Alt+key
            tokens.append(buf[i:i + 2])
            i += 2
    return tokens, buf[i:]


class Router:
    def __init__(self, leader=b"\x02", toggle="d", bare=False, message=False):
        self.leader, self.toggle = leader, toggle
        self.leader_name = "ctrl+" + chr(leader[0] + 96)
        self.bare = bare
        self.message = message  # a plugin-drawn message screen: bare keys always on, "r" retries
        self.pending = b""
        self.in_paste = False
        self.armed_at = None  # when the leader was pressed
        self.pending_at = None

    def feed(self, data, now):
        return self._run(self.pending + data, now, final=False)

    def poll(self, now):
        """Actions that are due without new input: a lone Esc, an expired leader."""
        out = []
        if self.pending and now - self.pending_at >= ESC_TIMEOUT:
            out += self._run(self.pending, now, final=True)
        if self.armed_at is not None and now - self.armed_at >= LEADER_TIMEOUT:
            self.armed_at = None
        return out

    def next_deadline(self):
        times = []
        if self.pending:
            times.append(self.pending_at + ESC_TIMEOUT)
        if self.armed_at is not None:
            times.append(self.armed_at + LEADER_TIMEOUT)
        return min(times) if times else None

    def _run(self, buf, now, final):
        actions = []
        if self.in_paste:
            end = buf.find(PASTE_END)
            if end < 0:
                self.pending = b""
                return [Action("forward", buf)]
            self.in_paste = False
            actions.append(Action("forward", buf[:end + len(PASTE_END)]))
            buf = buf[end + len(PASTE_END):]
        tokens, rest = split(buf, final)
        for token in tokens:
            if token == PASTE_START:
                self.in_paste = True
                actions.append(Action("forward", token))
                continue
            if self.in_paste:
                actions.append(Action("forward", token))
                if token == PASTE_END:
                    self.in_paste = False
                continue
            actions.append(self._token(token, now))
        self.pending = rest
        if not rest:
            self.pending_at = None
        elif self.pending_at is None:
            self.pending_at = now
        return _merge([a for a in actions if a is not None])

    def _token(self, token, now):
        decoded = decode(token)
        name, release = (decoded if isinstance(decoded, tuple) else (decoded, False))
        if self.armed_at is not None:
            if release:
                return None
            self.armed_at = None
            if name == self.toggle:
                return Action("close")
            return {
                "tab": Action("next"), "shift+tab": Action("prev"),
                "p": Action("personal"), "w": Action("work"), "r": Action("retry"),
                self.leader_name: Action("forward", self.leader),
            }.get(name)
        if name == self.leader_name:
            if release:
                return None
            self.armed_at = now
            return None
        if self.bare or self.message:
            if name in ("tab", "shift+tab", "esc"):
                if release:
                    return None
                return {"tab": Action("next"), "shift+tab": Action("prev"), "esc": Action("close")}[name]
        if self.message and name in ("r", "enter"):
            return Action("retry")
        return Action("forward", token)


def _merge(actions):
    merged = []
    for a in actions:
        if merged and a.kind == "forward" and merged[-1].kind == "forward":
            merged[-1] = Action("forward", merged[-1].data + a.data)
        else:
            merged.append(a)
    return merged

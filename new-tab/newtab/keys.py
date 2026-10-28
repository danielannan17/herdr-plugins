"""Terminal input bytes to key names. Pure apart from the clock-free parser state.

Key names: a single printable character is itself; everything else is a name
such as "enter", "esc", "up", "alt+down", "ctrl+u".
"""
from __future__ import annotations

import codecs

ESC = "\x1b"

_CSI_FINAL = {
    "A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end",
}
_TILDE = {"1": "home", "3": "delete", "4": "end", "7": "home", "8": "end"}
_CSI_U = {13: "enter", 27: "esc", 9: "tab", 127: "backspace", 8: "backspace"}


def _modifier_prefix(param: str) -> str:
    """xterm modifier parameter: 1 + bitmask(shift=1, alt=2, ctrl=4, meta=8)."""
    try:
        bits = int(param) - 1
    except ValueError:
        return ""
    parts = []
    if bits & 4:
        parts.append("ctrl")
    if bits & (2 | 8):
        parts.append("alt")
    if bits & 1:
        parts.append("shift")
    return "+".join(parts)


def _with_mod(mod: str, name: str) -> str:
    return f"{mod}+{name}" if mod else name


def _csi(params: str, final: str) -> str:
    fields = params.split(";")
    mod = _modifier_prefix(fields[1]) if len(fields) > 1 and fields[1] else ""
    if final in _CSI_FINAL:
        return _with_mod(mod, _CSI_FINAL[final])
    if final == "~":
        name = _TILDE.get(fields[0])
        return _with_mod(mod, name) if name else "unknown"
    if final == "Z":
        return "shift+tab"
    if final == "u":
        try:
            code = int(fields[0].split(":")[0])
        except ValueError:
            return "unknown"
        name = _CSI_U.get(code)
        if name is None:
            if code >= 32 and chr(code).isprintable():
                ch = chr(code)
                # Shift is already folded into the codepoint for text keys.
                mod = mod.replace("shift", "").strip("+")
                return _with_mod(mod, ch) if mod else ch
            return "unknown"
        return _with_mod(mod, name)
    return "unknown"


def _control(ch: str) -> str:
    code = ord(ch)
    if ch in "\r\n":
        return "enter"
    if ch == "\t":
        return "tab"
    if ch in ("\x7f", "\x08"):
        return "backspace"
    if 1 <= code <= 26:
        return "ctrl+" + chr(code + 96)
    return "unknown"


class KeyParser:
    """Incremental parser. `pending` is true while a lone ESC or a partial
    escape sequence is buffered; the caller waits a short timeout and calls
    `flush()` to resolve it (a lone ESC is the Escape key)."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buf = ""

    @property
    def pending(self) -> bool:
        return bool(self._buf)

    def feed(self, data: bytes) -> list[str]:
        self._buf += self._decoder.decode(data)
        return self._drain()

    def flush(self) -> list[str]:
        """Resolve buffered input after the escape timeout."""
        keys = self._drain()
        while self._buf:
            if self._buf[0] != ESC:
                self._buf = self._buf[1:]
                continue
            keys.append("esc")
            rest = self._buf[1:]
            # A truncated CSI/SS3 sequence is dropped rather than typed as text.
            self._buf = "" if rest[:1] in ("[", "O") else rest
            keys.extend(self._drain())
        return keys

    def _drain(self) -> list[str]:
        keys: list[str] = []
        buf = self._buf
        i = 0
        while i < len(buf):
            ch = buf[i]
            if ch != ESC:
                keys.append(ch if ch.isprintable() else _control(ch))
                i += 1
                continue
            rest = buf[i + 1:]
            if not rest:
                break
            if rest[0] == "[":
                j = 1
                while j < len(rest) and not ("@" <= rest[j] <= "~"):
                    j += 1
                if j >= len(rest):
                    break
                keys.append(_csi(rest[1:j], rest[j]))
                i += 1 + j + 1
            elif rest[0] == "O":
                if len(rest) < 2:
                    break
                keys.append(_CSI_FINAL.get(rest[1], "unknown"))
                i += 3
            elif rest[0] == ESC:
                # Meta-as-ESC prefix: ESC ESC [ B is Alt+Down.
                sub = KeyParser()
                sub._buf = rest
                inner = sub._drain()
                if not inner:
                    break
                first = inner[0]
                keys.append(first if first.startswith("alt+") else "alt+" + first)
                keys.extend(inner[1:])
                i += 1 + len(rest) - len(sub._buf)
            else:
                # ESC followed by a character: Alt+character.
                nxt = rest[0]
                name = nxt if nxt.isprintable() else _control(nxt)
                keys.append("alt+" + name)
                i += 2
        self._buf = buf[i:]
        return keys
